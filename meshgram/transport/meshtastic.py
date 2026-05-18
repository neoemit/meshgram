from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from typing import Any, Optional

from meshtastic import BROADCAST_ADDR
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from .._mesh_helpers import (
    extract_optional_int,
    extract_reaction_emoji,
    is_broadcast_destination,
    is_text_message_portnum,
    node_num_to_id,
    normalize_node_id,
    sanitize_reaction_emoji_text,
)
from ..config import MeshgramSettings
from ..types import (
    MeshReactionEvent,
    MeshTextEvent,
    SendMeshAction,
    SendMeshReactionAction,
)
from . import MeshReactionCallback, MeshTextCallback, MeshTransport

LOGGER = logging.getLogger(__name__)

DEFAULT_MESHTASTIC_PAYLOAD_LIMIT = 233

try:
    from meshtastic.protobuf import mesh_pb2
except ModuleNotFoundError:
    try:
        import meshtastic.mesh_pb2 as mesh_pb2  # type: ignore[attr-defined]
    except ModuleNotFoundError:
        mesh_pb2 = None  # type: ignore[assignment]

try:
    from meshtastic.protobuf import portnums_pb2
except ModuleNotFoundError:
    try:
        import meshtastic.portnums_pb2 as portnums_pb2  # type: ignore[attr-defined]
    except ModuleNotFoundError:
        portnums_pb2 = None  # type: ignore[assignment]


class MeshtasticTransport(MeshTransport):
    backend_name = "meshtastic"
    supports_reactions = True
    supports_reply_threading = True
    supports_wait_for_ack = True

    def __init__(self, settings: MeshgramSettings):
        super().__init__(settings)
        self.iface: Any = None
        self._supports_sendtext_reply_id: Optional[bool] = None
        self._supports_senddata_reply_id: Optional[bool] = None
        self._supports_lowlevel_packet: Optional[bool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_text: Optional[MeshTextCallback] = None
        self._on_reaction: Optional[MeshReactionCallback] = None

    # --- Lifecycle ----------------------------------------------------------

    @property
    def payload_limit(self) -> int:
        if mesh_pb2 is None:
            return DEFAULT_MESHTASTIC_PAYLOAD_LIMIT

        constants = getattr(mesh_pb2, "Constants", None)
        payload_len = getattr(constants, "DATA_PAYLOAD_LEN", None)
        if payload_len is None:
            return DEFAULT_MESHTASTIC_PAYLOAD_LIMIT

        return int(payload_len)

    async def connect(
        self,
        loop: asyncio.AbstractEventLoop,
        on_text: MeshTextCallback,
        on_reaction: MeshReactionCallback,
    ) -> None:
        self._loop = loop
        self._on_text = on_text
        self._on_reaction = on_reaction

        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        connection = self.settings.meshtastic.connection
        if connection.mode == "tcp":
            LOGGER.info(
                "Connecting to Meshtastic over TCP: %s:%s",
                connection.tcp_host,
                connection.tcp_port,
            )
            self.iface = self._create_tcp_interface(connection.tcp_host, connection.tcp_port, connection.no_nodes)
        else:
            if connection.serial_device:
                LOGGER.info("Connecting to Meshtastic serial device: %s", connection.serial_device)
            else:
                LOGGER.info("Connecting to Meshtastic serial device via auto-detect")
            self.iface = self._create_serial_interface(connection.serial_device, connection.no_nodes)

        pub.subscribe(self._on_receive, "meshtastic.receive")
        self.refresh_local_node_id()

    def _create_tcp_interface(self, host: str, port: int, no_nodes: bool):
        kwargs = {
            "hostname": host,
            "portNumber": port,
        }
        if no_nodes:
            kwargs["noNodes"] = True

        try:
            return meshtastic.tcp_interface.TCPInterface(**kwargs)
        except TypeError as exc:
            if "noNodes" in str(exc):
                LOGGER.info("Meshtastic TCPInterface does not support noNodes; retrying without it")
                return meshtastic.tcp_interface.TCPInterface(hostname=host, portNumber=port)
            raise

    def _create_serial_interface(self, device: Optional[str], no_nodes: bool):
        kwargs = {"devPath": device}
        if no_nodes:
            kwargs["noNodes"] = True

        try:
            return meshtastic.serial_interface.SerialInterface(**kwargs)
        except TypeError as exc:
            if "noNodes" in str(exc):
                LOGGER.info("Meshtastic SerialInterface does not support noNodes; retrying without it")
                return meshtastic.serial_interface.SerialInterface(devPath=device)
            raise

    @property
    def is_connected(self) -> bool:
        return self.iface is not None

    def wait_for_ack_sync(self) -> None:
        iface_wait = getattr(self.iface, "waitForAckNak", None) if self.iface is not None else None
        if not callable(iface_wait):
            return
        iface_wait()

    async def wait_for_ack(self) -> None:
        if self.iface is None:
            return
        await asyncio.to_thread(self.wait_for_ack_sync)

    @property
    def supports_wait_for_ack_runtime(self) -> bool:
        return self.iface is not None and callable(getattr(self.iface, "waitForAckNak", None))

    def invalidate_connection(self) -> None:
        if self.iface is not None:
            with contextlib.suppress(Exception):
                self.iface.close()
        self.iface = None
        self.local_node_id = None
        self._supports_sendtext_reply_id = None
        self._supports_senddata_reply_id = None
        self._supports_lowlevel_packet = None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            pub.unsubscribe(self._on_receive, "meshtastic.receive")
        self.invalidate_connection()

    def refresh_local_node_id(self) -> None:
        if self.iface is None:
            return

        try:
            user = self.iface.getMyUser()
        except Exception:
            return

        if isinstance(user, dict):
            maybe_id = user.get("id")
            if isinstance(maybe_id, str) and maybe_id:
                self.local_node_id = maybe_id

    # --- Inbound dispatch ---------------------------------------------------

    def _on_receive(self, packet, interface) -> None:  # noqa: ARG002 - pubsub signature
        if self._loop is None:
            return

        text_event = self._build_text_event(packet)
        reaction_event = self._build_reaction_event(packet)

        if reaction_event is not None and self._on_reaction is not None:
            self._loop.call_soon_threadsafe(
                functools.partial(self._schedule_callback, self._on_reaction(reaction_event))
            )
            return
        if text_event is not None and self._on_text is not None:
            self._loop.call_soon_threadsafe(
                functools.partial(self._schedule_callback, self._on_text(text_event))
            )

    def _schedule_callback(self, coro) -> None:
        assert self._loop is not None
        task = self._loop.create_task(coro)
        task.add_done_callback(_log_task_exception)

    def _build_text_event(self, packet: dict[str, Any]) -> Optional[MeshTextEvent]:
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return None

        if not is_text_message_portnum(decoded.get("portnum")):
            return None
        if extract_reaction_emoji(decoded) is not None:
            return None

        payload = decoded.get("payload", b"")
        if isinstance(payload, bytes):
            payload_bytes = payload
        elif isinstance(payload, str):
            payload_bytes = payload.encode("utf-8", errors="ignore")
        else:
            return None

        text = payload_bytes.decode(errors="ignore")
        if not text.strip():
            return None

        from_num = extract_optional_int(packet.get("from"))
        to_num = extract_optional_int(packet.get("to"))

        from_id = normalize_node_id(packet.get("fromId"), fallback_num=from_num)
        to_id = normalize_node_id(packet.get("toId"), fallback_num=to_num)

        try:
            channel_index = int(packet.get("channel", 0))
        except (TypeError, ValueError):
            channel_index = 0

        packet_id = extract_optional_int(packet.get("id"))

        reply_id = extract_optional_int(decoded.get("replyId"))
        if reply_id is None:
            reply_id = extract_optional_int(decoded.get("reply_id"))

        sender_label = self.resolve_sender_label(from_id, from_num=from_num)

        return MeshTextEvent(
            from_id=from_id,
            to_id=to_id,
            packet_id=packet_id,
            reply_id=reply_id,
            channel_index=channel_index,
            text=text,
            sender_label=sender_label,
            raw_packet=packet,
        )

    def _build_reaction_event(self, packet: dict[str, Any]) -> Optional[MeshReactionEvent]:
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return None
        if not is_text_message_portnum(decoded.get("portnum")):
            return None

        emoji = extract_reaction_emoji(decoded)
        if emoji is None:
            return None

        target_packet_id = extract_optional_int(decoded.get("replyId"))
        if target_packet_id is None:
            target_packet_id = extract_optional_int(decoded.get("reply_id"))
        if target_packet_id is None:
            target_packet_id = extract_optional_int(packet.get("replyId"))
        if target_packet_id is None:
            target_packet_id = extract_optional_int(packet.get("reply_id"))
        if target_packet_id is None:
            return None

        from_num = extract_optional_int(packet.get("from"))
        to_num = extract_optional_int(packet.get("to"))
        from_id = normalize_node_id(packet.get("fromId"), fallback_num=from_num)
        to_id = normalize_node_id(packet.get("toId"), fallback_num=to_num)

        try:
            channel_index = int(packet.get("channel", 0))
        except (TypeError, ValueError):
            channel_index = 0

        packet_id = extract_optional_int(packet.get("id"))
        sender_label = self.resolve_sender_label(from_id, from_num=from_num)

        return MeshReactionEvent(
            from_id=from_id,
            to_id=to_id,
            packet_id=packet_id,
            target_packet_id=target_packet_id,
            channel_index=channel_index,
            emoji=emoji,
            sender_label=sender_label,
            raw_packet=packet,
        )

    # --- Sender label resolution -------------------------------------------

    def resolve_sender_label(self, from_id: Optional[str], from_num: Optional[int] = None) -> str:
        override_label = self._resolve_override_label(from_id, from_num)
        if override_label:
            return override_label

        node_info = self._find_node_info(from_id, from_num)
        if isinstance(node_info, dict):
            user = node_info.get("user", {})
            if isinstance(user, dict):
                short_name = user.get("shortName")
                if isinstance(short_name, str) and short_name.strip():
                    return short_name.strip()

                long_name = user.get("longName")
                if isinstance(long_name, str) and long_name.strip():
                    return long_name.strip()

        fallback_id = from_id
        if fallback_id is None and from_num is not None:
            fallback_id = node_num_to_id(from_num)
        if fallback_id is not None:
            return fallback_id
        return "unknown"

    def _resolve_override_label(self, from_id: Optional[str], from_num: Optional[int]) -> Optional[str]:
        overrides = self.settings.meshtastic.node_name_overrides
        if not overrides:
            return None

        candidates: list[str] = []
        if from_id:
            normalized = normalize_node_id(from_id)
            for value in (from_id, normalized):
                if not value:
                    continue
                candidates.append(value)
                if value.startswith("!"):
                    candidates.append(value[1:])

        if from_num is not None:
            normalized_num = from_num & 0xFFFFFFFF
            normalized_id = node_num_to_id(normalized_num)
            candidates.extend(
                [
                    str(from_num),
                    str(normalized_num),
                    normalized_id,
                    normalized_id[1:],
                    f"0x{normalized_num:08x}",
                ]
            )

        for candidate in candidates:
            direct = overrides.get(candidate)
            if direct:
                return direct

        normalized_candidates = {candidate.strip().lower() for candidate in candidates if candidate.strip()}
        for key, value in overrides.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in normalized_candidates:
                return value

        return None

    def _find_node_info(self, from_id: Optional[str], from_num: Optional[int]) -> Optional[dict[str, Any]]:
        if self.iface is None:
            return None

        nodes = getattr(self.iface, "nodes", {})
        if not isinstance(nodes, dict):
            return None

        normalized_id = normalize_node_id(from_id) if from_id else None
        normalized_num = (from_num & 0xFFFFFFFF) if from_num is not None else None

        candidate_keys: list[Any] = []
        if normalized_id:
            candidate_keys.extend([normalized_id, normalized_id[1:]])
        if from_id:
            candidate_keys.append(from_id)
        if normalized_num is not None:
            candidate_keys.extend([normalized_num, str(normalized_num)])

        for key in candidate_keys:
            node_info = nodes.get(key)
            if isinstance(node_info, dict):
                return node_info

        for key, node_info in nodes.items():
            if not isinstance(node_info, dict):
                continue

            if normalized_id is not None and isinstance(key, str):
                key_id = normalize_node_id(key)
                if key_id == normalized_id:
                    return node_info

            user = node_info.get("user", {})
            if isinstance(user, dict):
                if normalized_id is not None:
                    user_id = user.get("id")
                    if isinstance(user_id, str) and normalize_node_id(user_id) == normalized_id:
                        return node_info

                if normalized_num is not None:
                    user_num = extract_optional_int(user.get("num"))
                    if user_num is not None and (user_num & 0xFFFFFFFF) == normalized_num:
                        return node_info

            if normalized_num is not None:
                node_num = extract_optional_int(node_info.get("num"))
                if node_num is not None and (node_num & 0xFFFFFFFF) == normalized_num:
                    return node_info

        return None

    # --- Outbound -----------------------------------------------------------

    def send_text(self, action: SendMeshAction):
        """Synchronous send. Kept for tests; production code uses :meth:`asend_text`."""
        if self.iface is None:
            raise RuntimeError("Meshtastic interface is not connected")

        destination_id = action.destination_id if action.destination_id is not None else BROADCAST_ADDR
        effective_want_ack = action.want_ack and not is_broadcast_destination(destination_id)
        if action.want_ack and not effective_want_ack:
            LOGGER.info(
                "Disabling Meshtastic wantAck for broadcast destination to avoid false ACK waits/retries"
            )
        kwargs = {
            "destinationId": destination_id,
            "channelIndex": action.channel_index,
            "wantAck": effective_want_ack,
        }

        if action.reply_id is None:
            return self.iface.sendText(action.text, **kwargs)

        if self._supports_sendtext_reply_id is not False:
            try:
                self._supports_sendtext_reply_id = True
                return self.iface.sendText(action.text, replyId=action.reply_id, **kwargs)
            except TypeError as exc:
                if "replyId" not in str(exc):
                    raise
                self._supports_sendtext_reply_id = False
                LOGGER.info(
                    "Meshtastic sendText does not support replyId; trying compatibility fallback"
                )

        if self._supports_senddata_reply_id is not False and portnums_pb2 is not None:
            send_data = getattr(self.iface, "sendData", None)
            if callable(send_data):
                try:
                    self._supports_senddata_reply_id = True
                    return send_data(
                        action.text.encode("utf-8"),
                        destinationId=destination_id,
                        portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                        wantAck=effective_want_ack,
                        channelIndex=action.channel_index,
                        replyId=action.reply_id,
                    )
                except TypeError as exc:
                    if "replyId" not in str(exc):
                        raise
                    self._supports_senddata_reply_id = False
                    LOGGER.info(
                        "Meshtastic sendData does not support replyId; trying low-level fallback"
                    )

        if self._can_use_lowlevel_packet():
            return self._send_text_packet_lowlevel(
                text=action.text,
                destination_id=destination_id,
                channel_index=action.channel_index,
                want_ack=effective_want_ack,
                reply_id=action.reply_id,
            )

        LOGGER.warning(
            "Reply threading fallback unavailable; sending message without reply linkage"
        )
        return self.iface.sendText(action.text, **kwargs)

    def send_reaction(self, action: SendMeshReactionAction):
        if self.iface is None:
            raise RuntimeError("Meshtastic interface is not connected")
        normalized_emoji = sanitize_reaction_emoji_text(action.emoji)
        if not normalized_emoji:
            raise ValueError("Reaction emoji cannot be empty")

        destination_id = action.destination_id if action.destination_id is not None else BROADCAST_ADDR
        if self._can_use_lowlevel_packet():
            return self._send_text_packet_lowlevel(
                text=normalized_emoji,
                destination_id=destination_id,
                channel_index=action.channel_index,
                want_ack=action.want_ack,
                reply_id=action.target_packet_id,
                emoji_codepoint=ord(normalized_emoji[0]),
            )

        LOGGER.warning(
            "Low-level Meshtastic packet API unavailable; sending reaction as plain reply text"
        )
        return self.send_text(
            SendMeshAction(
                text=normalized_emoji,
                destination_id=destination_id,
                channel_index=action.channel_index,
                reply_id=action.target_packet_id,
                want_ack=action.want_ack,
            )
        )

    async def asend_text(self, action: SendMeshAction):
        return self.send_text(action)

    async def asend_reaction(self, action: SendMeshReactionAction):
        return self.send_reaction(action)

    def _can_use_lowlevel_packet(self) -> bool:
        if self._supports_lowlevel_packet is None:
            self._supports_lowlevel_packet = (
                mesh_pb2 is not None
                and portnums_pb2 is not None
                and callable(getattr(self.iface, "_sendPacket", None))
            )
        return self._supports_lowlevel_packet

    def _send_text_packet_lowlevel(
        self,
        text: str,
        destination_id: Any,
        channel_index: int,
        want_ack: bool,
        reply_id: Optional[int] = None,
        emoji_codepoint: Optional[int] = None,
    ):
        if not self._can_use_lowlevel_packet():
            raise RuntimeError("Low-level packet send is unavailable")
        if mesh_pb2 is None or portnums_pb2 is None:
            raise RuntimeError("Meshtastic protobuf modules are unavailable")

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.channel = channel_index
        mesh_packet.decoded.payload = text.encode("utf-8")
        mesh_packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        if reply_id is not None:
            mesh_packet.decoded.reply_id = reply_id
        if emoji_codepoint is not None:
            mesh_packet.decoded.emoji = emoji_codepoint

        return self.iface._sendPacket(
            mesh_packet,
            destinationId=destination_id,
            wantAck=want_ack,
        )


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exception = task.exception()
        if exception:
            LOGGER.error(
                "Meshtastic dispatch failed: %s",
                exception,
                exc_info=(type(exception), exception, exception.__traceback__),
            )


# Backward-compatible alias for older imports.
MeshtasticClient = MeshtasticTransport

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from typing import Any, Optional

from ..config import MeshgramSettings
from ..types import (
    MeshPacketRef,
    MeshReactionEvent,
    MeshTextEvent,
    SendMeshAction,
    SendMeshReactionAction,
)
from . import MeshReactionCallback, MeshTextCallback, MeshTransport

LOGGER = logging.getLogger(__name__)


DEFAULT_MESHCORE_PAYLOAD_LIMIT = 140
OUTBOUND_ECHO_DEDUPE_TTL_SECONDS = 30.0


class MeshCoreTransport(MeshTransport):
    backend_name = "meshcore"
    supports_reactions = False
    supports_reply_threading = False
    # ACK waiting is handled inside ``asend_text`` (per-message), so the app's
    # generic "wait_for_ack" pass after each send is a no-op.
    supports_wait_for_ack = False

    def __init__(self, settings: MeshgramSettings):
        super().__init__(settings)
        self._mc: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_text: Optional[MeshTextCallback] = None
        self._on_reaction: Optional[MeshReactionCallback] = None
        self._contacts: dict[str, dict[str, Any]] = {}
        self._subscriptions: list[Any] = []
        self.local_short_name: Optional[str] = None
        # text → monotonic-time-sent, used to suppress radio echoes of our own
        # outbound channel messages within ``OUTBOUND_ECHO_DEDUPE_TTL_SECONDS``.
        self._recent_outbound_texts: dict[tuple[int, str], float] = {}

    # --- Lifecycle ----------------------------------------------------------

    @property
    def payload_limit(self) -> int:
        return DEFAULT_MESHCORE_PAYLOAD_LIMIT

    @property
    def is_connected(self) -> bool:
        if self._mc is None:
            return False
        flag = getattr(self._mc, "is_connected", None)
        if isinstance(flag, bool):
            return flag
        return True

    async def connect(
        self,
        loop: asyncio.AbstractEventLoop,
        on_text: MeshTextCallback,
        on_reaction: MeshReactionCallback,
    ) -> None:
        try:
            from meshcore import EventType, MeshCore
        except ImportError as exc:  # pragma: no cover - guarded import
            raise RuntimeError(
                "The `meshcore` package is required for backend=meshcore; "
                "install with: pip install meshcore>=2.3.7"
            ) from exc

        self._loop = loop
        self._on_text = on_text
        self._on_reaction = on_reaction

        cfg = self.settings.meshcore.connection
        if cfg.mode == "tcp":
            LOGGER.info("Connecting to MeshCore over TCP: %s:%s", cfg.tcp_host, cfg.tcp_port)
            self._mc = await MeshCore.create_tcp(
                cfg.tcp_host,
                cfg.tcp_port,
                auto_reconnect=cfg.auto_reconnect,
            )
        elif cfg.mode == "ble":
            if not cfg.ble_address:
                raise ValueError("MeshCore BLE mode requires meshcore.connection.ble_address (or MESH_BLE_ADDRESS)")
            LOGGER.info("Connecting to MeshCore over BLE: %s", cfg.ble_address)
            ble_kwargs: dict[str, Any] = {}
            if cfg.ble_pin:
                ble_kwargs["pin"] = cfg.ble_pin
            self._mc = await MeshCore.create_ble(cfg.ble_address, **ble_kwargs)
        else:
            if cfg.serial_device:
                LOGGER.info(
                    "Connecting to MeshCore serial device: %s (baudrate=%s)",
                    cfg.serial_device,
                    cfg.baudrate,
                )
            else:
                LOGGER.info("Connecting to MeshCore serial device via auto-detect")
            self._mc = await MeshCore.create_serial(cfg.serial_device, cfg.baudrate)

        if self._mc is None or not getattr(self._mc, "is_connected", False) or getattr(self._mc, "commands", None) is None:
            handshake_hint = (
                "MeshCore handshake failed. Verify: (1) the device runs MeshCore companion "
                "firmware compiled with the matching transport; (2) MESH_BAUDRATE matches "
                "(common values: 115200, 921600); (3) you selected the right MESH_MODE "
                "(serial/tcp/ble) for this device. If the radio is actually Meshtastic, set "
                "MESH_BACKEND=meshtastic."
            )
            self._mc = None
            raise RuntimeError(handshake_hint)

        await self._refresh_local_node_async()
        await self._refresh_contacts_async()

        self._subscriptions.append(
            self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._handle_contact_msg)
        )
        self._subscriptions.append(
            self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._handle_channel_msg)
        )
        self._subscriptions.append(
            self._mc.subscribe(EventType.NEW_CONTACT, self._handle_new_contact)
        )

        await self._mc.start_auto_message_fetching()
        LOGGER.info("MeshCore transport ready (local_node_id=%s, contacts=%s)", self.local_node_id, len(self._contacts))

    def invalidate_connection(self) -> None:
        for sub in self._subscriptions:
            with contextlib.suppress(Exception):
                self._mc.unsubscribe(sub)
        self._subscriptions = []
        if self._mc is not None and self._loop is not None and not self._loop.is_closed():
            stopper = getattr(self._mc, "stop_auto_message_fetching", None)
            if callable(stopper):
                with contextlib.suppress(Exception):
                    coro = stopper()
                    if asyncio.iscoroutine(coro):
                        self._loop.create_task(coro)
            disconnect = getattr(self._mc, "disconnect", None)
            if callable(disconnect):
                with contextlib.suppress(Exception):
                    coro = disconnect()
                    if asyncio.iscoroutine(coro):
                        self._loop.create_task(coro)
        self._mc = None
        self.local_node_id = None

    def close(self) -> None:
        self.invalidate_connection()

    def refresh_local_node_id(self) -> None:
        # Sync-callable shim from app; the real refresh is async and runs at connect.
        if self._mc is None:
            return
        info = getattr(self._mc, "self_info", None)
        if isinstance(info, dict):
            self.local_node_id = self._derive_local_node_id(info)

    async def _refresh_local_node_async(self) -> None:
        info = getattr(self._mc, "self_info", None)
        if isinstance(info, dict):
            self.local_node_id = self._derive_local_node_id(info)
            for key in ("adv_name", "name", "shortName", "short_name"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    self.local_short_name = value.strip()
                    break

    async def _refresh_contacts_async(self) -> None:
        try:
            result = await self._mc.commands.get_contacts()
        except Exception as exc:
            LOGGER.warning("MeshCore get_contacts failed: %s", exc)
            return
        if getattr(result, "type", None) is None:
            return
        from meshcore import EventType

        if result.type == EventType.ERROR:
            LOGGER.warning("MeshCore get_contacts returned error: %s", result.payload)
            return
        payload = result.payload
        if isinstance(payload, dict):
            self._contacts = dict(payload)

    @staticmethod
    def _derive_local_node_id(info: dict[str, Any]) -> Optional[str]:
        pubkey = info.get("public_key")
        if isinstance(pubkey, str) and pubkey:
            return pubkey[:12]
        if isinstance(pubkey, (bytes, bytearray)):
            return pubkey.hex()[:12]
        return None

    # --- Inbound event handlers --------------------------------------------

    async def _handle_contact_msg(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        LOGGER.info("MeshCore inbound DM payload keys=%s", sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
        text = str(payload.get("text", "")).strip()
        if not text:
            return

        pubkey_prefix = str(payload.get("pubkey_prefix", "") or "").strip().lower()
        sender_label = self.resolve_sender_label(pubkey_prefix or None)
        timestamp = payload.get("timestamp")

        mesh_event = MeshTextEvent(
            from_id=pubkey_prefix or None,
            to_id=self.local_node_id,
            packet_id=self._synthetic_inbound_id("dm", pubkey_prefix, text, timestamp),
            reply_id=None,
            channel_index=-1,
            text=text,
            sender_label=sender_label,
            raw_packet=dict(payload),
        )

        if self._on_text is not None:
            await self._on_text(mesh_event)

    async def _handle_channel_msg(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        raw_text = str(payload.get("text", "")).strip()
        if not raw_text:
            return

        try:
            channel_index = int(payload.get("channel_idx", 0))
        except (TypeError, ValueError):
            channel_index = 0

        # MeshCore channel messages embed the sender in the body as "<name>: <text>".
        # Strip it so plugins see just the message; surface the name as sender_label.
        embedded_sender, body = _split_embedded_sender(raw_text)

        pubkey_prefix = str(payload.get("pubkey_prefix", "") or "").strip().lower()
        if embedded_sender:
            sender_label = embedded_sender
        elif pubkey_prefix:
            sender_label = self.resolve_sender_label(pubkey_prefix)
        else:
            sender_label = self._channel_sender_label(body, channel_index)
        timestamp = payload.get("sender_timestamp") or payload.get("timestamp")

        if self._is_local_echo(channel_index, embedded_sender, body):
            LOGGER.debug("Suppressing MeshCore channel echo of our own transmission: %r", raw_text[:80])
            return

        LOGGER.info(
            "MeshCore channel message: channel=%s sender=%r text=%r",
            channel_index,
            sender_label,
            body[:80],
        )

        mesh_event = MeshTextEvent(
            from_id=pubkey_prefix or None,
            to_id=None,
            packet_id=self._synthetic_inbound_id("ch", str(channel_index), body, timestamp),
            reply_id=None,
            channel_index=channel_index,
            text=body,
            sender_label=sender_label,
            raw_packet=dict(payload),
        )

        if self._on_text is not None:
            await self._on_text(mesh_event)

    async def _handle_new_contact(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        if not isinstance(payload, dict):
            return
        public_key = payload.get("public_key")
        if isinstance(public_key, str) and public_key:
            self._contacts[public_key] = dict(payload)

    @staticmethod
    def _synthetic_inbound_id(
        kind: str,
        scope: str,
        text: str,
        timestamp: Any,
    ) -> str:
        ts_part = str(timestamp) if timestamp is not None else f"now:{int(time.time() * 1000)}"
        digest = hashlib.sha1(f"{kind}|{scope}|{ts_part}|{text}".encode("utf-8")).hexdigest()[:16]
        return f"mc-{kind}-{digest}"

    # --- Outbound ----------------------------------------------------------

    async def asend_text(self, action: SendMeshAction) -> object:
        if self._mc is None:
            raise RuntimeError("MeshCore client is not connected")

        from meshcore import EventType

        is_dm = isinstance(action.destination_id, str) and action.destination_id.strip()
        result: Any
        if is_dm:
            dst = action.destination_id  # type: ignore[assignment]
            if action.reply_id is not None:
                LOGGER.debug(
                    "MeshCore backend has no native reply threading; dropping reply_id=%s",
                    action.reply_id,
                )
            result = await self._mc.commands.send_msg(dst, action.text)
        else:
            channel_index = action.channel_index if action.channel_index >= 0 else self.settings.meshcore.bridge_channel
            result = await self._mc.commands.send_chan_msg(channel_index, action.text)

        if getattr(result, "type", None) == EventType.ERROR:
            raise RuntimeError(f"MeshCore send failed: {result.payload}")

        expected_ack = self._extract_expected_ack_hex(result)

        if (
            action.wait_for_ack
            and action.want_ack
            and is_dm
            and expected_ack is not None
        ):
            timeout_seconds = max(1.0, action.ack_timeout_ms / 1000) if action.ack_timeout_ms else 10.0
            ack_event = await self._mc.wait_for_event(
                EventType.ACK,
                attribute_filters={"code": expected_ack},
                timeout=timeout_seconds,
            )
            if ack_event is None:
                raise TimeoutError(f"MeshCore ACK wait timed out for code {expected_ack}")

        # MeshCore channel sends don't return an expected_ack (broadcasts have no
        # per-recipient ACK). Synthesise an id so the app-level
        # ``require_packet_id`` check passes and we don't retry-transmit.
        packet_id = expected_ack or f"mc-out-{uuid.uuid4().hex[:12]}"

        # Remember the text so we suppress the radio's echo of our own transmission
        # when it arrives back as an inbound channel message.
        if not is_dm:
            channel_index = action.channel_index if action.channel_index >= 0 else self.settings.meshcore.bridge_channel
            self._record_outbound_text(channel_index, action.text)

        return {"id": packet_id}

    def _record_outbound_text(self, channel_index: int, text: str) -> None:
        now = time.monotonic()
        self._prune_outbound_cache(now)
        self._recent_outbound_texts[(channel_index, text.strip())] = now

    def _prune_outbound_cache(self, now: float) -> None:
        cutoff = now - OUTBOUND_ECHO_DEDUPE_TTL_SECONDS
        stale = [key for key, ts in self._recent_outbound_texts.items() if ts < cutoff]
        for key in stale:
            self._recent_outbound_texts.pop(key, None)

    def _is_local_echo(self, channel_index: int, embedded_sender: Optional[str], body: str) -> bool:
        # 1) Body match against recent outbound text (most reliable).
        self._prune_outbound_cache(time.monotonic())
        if (channel_index, body.strip()) in self._recent_outbound_texts:
            return True

        # 2) Sender name match against our local short_name, ignoring trust/role markers.
        if embedded_sender and self.local_short_name:
            stripped_sender = embedded_sender.split("•", 1)[0].strip().lower()
            if stripped_sender and stripped_sender == self.local_short_name.lower():
                return True

        # 3) Sender prefix match against local pubkey (rare on channels, defensive).
        if embedded_sender and self.local_node_id:
            sender_low = embedded_sender.lower()
            node_low = self.local_node_id.lower()
            if sender_low == node_low or sender_low == node_low[:12]:
                return True

        return False

    async def asend_reaction(self, action: SendMeshReactionAction) -> object:
        LOGGER.debug(
            "MeshCore backend does not support reactions; dropping emoji=%s target_packet_id=%s",
            action.emoji,
            action.target_packet_id,
        )
        return None

    @staticmethod
    def _extract_expected_ack_hex(result: Any) -> Optional[str]:
        payload = getattr(result, "payload", None)
        if not isinstance(payload, dict):
            return None
        raw = payload.get("expected_ack")
        if isinstance(raw, (bytes, bytearray)):
            return raw.hex()
        if isinstance(raw, str):
            return raw
        return None

    # --- Sender labels ------------------------------------------------------

    def resolve_sender_label(
        self,
        from_id: Optional[str],
        from_num: Optional[int] = None,  # noqa: ARG002 - MeshCore has no numeric node IDs
    ) -> str:
        if from_id:
            override = self.settings.meshcore.contact_name_overrides
            normalized = from_id.strip().lower()
            for key, value in override.items():
                if str(key).strip().lower() == normalized or normalized.startswith(str(key).strip().lower()):
                    return value

            for pubkey, contact in self._contacts.items():
                pk = pubkey.lower() if isinstance(pubkey, str) else ""
                if pk.startswith(normalized) or normalized.startswith(pk[:12]):
                    name = contact.get("adv_name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()

            return from_id

        return "unknown"

    @staticmethod
    def _channel_sender_label(text: str, channel_index: int) -> str:  # noqa: ARG004 - reserved for future heuristics
        return f"ch{channel_index}"


def _split_embedded_sender(raw_text: str) -> tuple[Optional[str], str]:
    """MeshCore channel messages arrive as ``"<short_name>: <text>"``. Split them."""
    delimiter = ": "
    idx = raw_text.find(delimiter)
    if idx <= 0 or idx > 32:
        return None, raw_text
    prefix = raw_text[:idx].strip()
    if not prefix or "\n" in prefix or "\r" in prefix:
        return None, raw_text
    body = raw_text[idx + len(delimiter):].strip()
    if not body:
        return None, raw_text
    return prefix, body

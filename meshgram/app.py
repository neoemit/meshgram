from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional, Sequence

from meshtastic import BROADCAST_ADDR
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub
from telegram import (
    Message,
    MessageReactionCountUpdated,
    MessageReactionUpdated,
    ReactionCount,
    ReactionTypeEmoji,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from .config import MeshgramSettings, load_settings
from .plugin import LoadedPlugin, load_plugins
from .reply_links import ReplyLinkRegistry
from .types import (
    MeshtasticReactionEvent,
    MeshtasticTextEvent,
    PluginAction,
    PluginContext,
    SendMeshtasticReactionAction,
    SendMeshtasticAction,
    SendTelegramReactionAction,
    SendTelegramAction,
    TelegramMessageEvent,
    TelegramReactionEvent,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_MESHTASTIC_PAYLOAD_LIMIT = 233
MESHTASTIC_PACKET_ID_DEDUPE_TTL_SECONDS = 120.0
TELEGRAM_REACTION_WRITE_DEDUPE_TTL_SECONDS = 12.0
DEFAULT_TELEGRAM_REACTION_FALLBACK_EMOJI = "👍"
TELEGRAM_REACTION_INVALID_ERROR_TOKEN = "reaction_invalid"

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


class MeshtasticClient:
    def __init__(self, settings: MeshgramSettings):
        self.settings = settings
        self.iface: Any = None
        self.local_node_id: Optional[str] = None
        self._packet_callback = None
        self._supports_sendtext_reply_id: Optional[bool] = None
        self._supports_senddata_reply_id: Optional[bool] = None
        self._supports_lowlevel_packet: Optional[bool] = None

    @property
    def payload_limit(self) -> int:
        if mesh_pb2 is None:
            return DEFAULT_MESHTASTIC_PAYLOAD_LIMIT

        constants = getattr(mesh_pb2, "Constants", None)
        payload_len = getattr(constants, "DATA_PAYLOAD_LEN", None)
        if payload_len is None:
            return DEFAULT_MESHTASTIC_PAYLOAD_LIMIT

        return int(payload_len)

    def connect(self, packet_callback) -> None:
        self._packet_callback = packet_callback

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

    def invalidate_connection(self) -> None:
        if self.iface is not None:
            with contextlib.suppress(Exception):
                self.iface.close()
        self.iface = None
        self.local_node_id = None
        self._supports_sendtext_reply_id = None
        self._supports_senddata_reply_id = None
        self._supports_lowlevel_packet = None

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

    def _on_receive(self, packet, interface) -> None:
        if self._packet_callback is not None:
            self._packet_callback(packet)

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
            fallback_id = _node_num_to_id(from_num)
        if fallback_id is not None:
            return fallback_id
        return "unknown"

    def _resolve_override_label(self, from_id: Optional[str], from_num: Optional[int]) -> Optional[str]:
        overrides = self.settings.meshtastic.node_name_overrides
        if not overrides:
            return None

        candidates: list[str] = []
        if from_id:
            normalized = _normalize_node_id(from_id)
            for value in (from_id, normalized):
                if not value:
                    continue
                candidates.append(value)
                if value.startswith("!"):
                    candidates.append(value[1:])

        if from_num is not None:
            normalized_num = from_num & 0xFFFFFFFF
            normalized_id = _node_num_to_id(normalized_num)
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

        normalized_id = _normalize_node_id(from_id) if from_id else None
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
                key_id = _normalize_node_id(key)
                if key_id == normalized_id:
                    return node_info

            user = node_info.get("user", {})
            if isinstance(user, dict):
                if normalized_id is not None:
                    user_id = user.get("id")
                    if isinstance(user_id, str) and _normalize_node_id(user_id) == normalized_id:
                        return node_info

                if normalized_num is not None:
                    user_num = _extract_optional_int(user.get("num"))
                    if user_num is not None and (user_num & 0xFFFFFFFF) == normalized_num:
                        return node_info

            if normalized_num is not None:
                node_num = _extract_optional_int(node_info.get("num"))
                if node_num is not None and (node_num & 0xFFFFFFFF) == normalized_num:
                    return node_info

        return None

    def send_text(self, action: SendMeshtasticAction):
        if self.iface is None:
            raise RuntimeError("Meshtastic interface is not connected")

        destination_id = action.destination_id if action.destination_id is not None else BROADCAST_ADDR
        kwargs = {
            "destinationId": destination_id,
            "channelIndex": action.channel_index,
            "wantAck": action.want_ack,
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
                        wantAck=action.want_ack,
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
                want_ack=action.want_ack,
                reply_id=action.reply_id,
            )

        LOGGER.warning(
            "Reply threading fallback unavailable; sending message without reply linkage"
        )
        return self.iface.sendText(action.text, **kwargs)

    def send_reaction(self, action: SendMeshtasticReactionAction):
        if self.iface is None:
            raise RuntimeError("Meshtastic interface is not connected")
        if not action.emoji:
            raise ValueError("Reaction emoji cannot be empty")

        destination_id = action.destination_id if action.destination_id is not None else BROADCAST_ADDR
        if self._can_use_lowlevel_packet():
            return self._send_text_packet_lowlevel(
                text="",
                destination_id=destination_id,
                channel_index=action.channel_index,
                want_ack=action.want_ack,
                reply_id=action.target_packet_id,
                emoji_codepoint=ord(action.emoji[0]),
            )

        LOGGER.warning(
            "Low-level Meshtastic packet API unavailable; sending reaction as plain reply text"
        )
        return self.send_text(
            SendMeshtasticAction(
                text=action.emoji,
                destination_id=destination_id,
                channel_index=action.channel_index,
                reply_id=action.target_packet_id,
                want_ack=action.want_ack,
            )
        )

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
        destination_id: str | int,
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

    def close(self) -> None:
        with contextlib.suppress(Exception):
            pub.unsubscribe(self._on_receive, "meshtastic.receive")
        self.invalidate_connection()


class MeshgramApp:
    def __init__(self, settings: MeshgramSettings):
        self.settings = settings
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bot_app: Optional[Application] = None
        self.meshtastic = MeshtasticClient(settings)
        self.plugins: list[LoadedPlugin] = load_plugins(settings.plugins)
        self.reply_links = ReplyLinkRegistry(
            ttl_hours=_get_bridge_reply_ttl_hours(settings),
        )
        self._mesh_connect_task: Optional[asyncio.Task[None]] = None
        self._seen_meshtastic_packet_ids: dict[int, float] = {}
        self._meshtastic_send_lock = asyncio.Lock()
        self._telegram_reaction_counts: dict[tuple[int, int], dict[str, int]] = {}
        self._recent_telegram_reaction_writes: dict[tuple[int, int, str], float] = {}

    async def _post_init(self, app: Application) -> None:
        self.loop = asyncio.get_running_loop()
        self._mesh_connect_task = asyncio.create_task(self._ensure_meshtastic_connected())

        await self._dispatch_startup()
        LOGGER.info("Meshgram runtime initialized")

    async def _ensure_meshtastic_connected(self) -> None:
        retry_delay_seconds = 5
        healthy_poll_seconds = 2

        while True:
            if self.meshtastic.is_connected:
                await asyncio.sleep(healthy_poll_seconds)
                continue

            try:
                self.meshtastic.connect(self._on_meshtastic_packet)
                LOGGER.info("Meshtastic connection established")
            except Exception as exc:
                self.meshtastic.invalidate_connection()
                LOGGER.warning(
                    "Meshtastic connection failed (%s). Retrying in %ss.",
                    exc,
                    retry_delay_seconds,
                )
                await asyncio.sleep(retry_delay_seconds)

    async def _dispatch_startup(self) -> None:
        context = self._plugin_context()
        for loaded_plugin in self.plugins:
            try:
                actions = await loaded_plugin.instance.on_startup(context)
            except Exception:
                LOGGER.exception("Plugin %s failed during startup", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    def _plugin_context(self) -> PluginContext:
        self.meshtastic.refresh_local_node_id()
        return PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=self.meshtastic.payload_limit,
            local_node_id=self.meshtastic.local_node_id,
            reply_links=self.reply_links,
        )

    async def _handle_telegram_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        if not message:
            return

        chat = message.chat
        if not chat:
            return

        text: Optional[str] = None
        text_source: Optional[str] = None

        if message.text is not None:
            text = message.text
            text_source = "text"
        elif message.caption is not None:
            text = message.caption
            text_source = "caption"

        from_user = message.from_user
        sender_display_name = "Unknown"
        is_from_bot = False
        if from_user is not None:
            sender_display_name = (
                from_user.full_name or from_user.username or str(from_user.id)
            )
            is_from_bot = bool(from_user.is_bot)

        event = TelegramMessageEvent(
            chat_id=chat.id,
            message_id=message.message_id,
            reply_to_message_id=_extract_telegram_reply_to_message_id(message),
            text=text,
            text_source=text_source,
            is_from_bot=is_from_bot,
            sender_display_name=sender_display_name,
            has_media=_message_has_media(message),
            raw_message=message,
        )

        await self._dispatch_telegram_message(event)

    async def _handle_telegram_reaction(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        event = self._build_telegram_reaction_event(update)
        if event is None:
            return

        await self._dispatch_telegram_reaction(event)

    def _build_telegram_reaction_event(self, update: Update) -> Optional[TelegramReactionEvent]:
        reaction_update = update.message_reaction
        if reaction_update is not None:
            return self._build_telegram_reaction_event_from_update(reaction_update)

        reaction_count_update = update.message_reaction_count
        if reaction_count_update is not None:
            return self._build_telegram_reaction_event_from_count_update(reaction_count_update)

        return None

    def _build_telegram_reaction_event_from_update(
        self,
        reaction_update: MessageReactionUpdated,
    ) -> Optional[TelegramReactionEvent]:
        chat = reaction_update.chat
        if chat is None:
            return None

        emoji = _extract_first_unicode_reaction_emoji(reaction_update)
        if emoji is None:
            return None

        user = reaction_update.user
        is_from_bot = bool(user and user.is_bot)

        return TelegramReactionEvent(
            chat_id=chat.id,
            message_id=reaction_update.message_id,
            emoji=emoji,
            is_from_bot=is_from_bot,
            raw_reaction=reaction_update,
        )

    def _build_telegram_reaction_event_from_count_update(
        self,
        reaction_count_update: MessageReactionCountUpdated,
    ) -> Optional[TelegramReactionEvent]:
        chat = reaction_count_update.chat
        if chat is None:
            return None

        emoji = self._extract_incremented_unicode_emoji_from_count_update(reaction_count_update)
        if emoji is None:
            return None

        if self._was_recent_telegram_reaction_write(chat.id, reaction_count_update.message_id, emoji):
            LOGGER.debug(
                "Ignoring Telegram reaction count echo from recent bot write: chat=%s message=%s emoji=%s",
                chat.id,
                reaction_count_update.message_id,
                emoji,
            )
            return None

        return TelegramReactionEvent(
            chat_id=chat.id,
            message_id=reaction_count_update.message_id,
            emoji=emoji,
            is_from_bot=False,
            raw_reaction=reaction_count_update,
        )

    def _extract_incremented_unicode_emoji_from_count_update(
        self,
        reaction_count_update: MessageReactionCountUpdated,
    ) -> Optional[str]:
        chat_id = reaction_count_update.chat.id
        message_id = reaction_count_update.message_id
        key = (chat_id, message_id)

        current_counts = _extract_unicode_emoji_counts(reaction_count_update.reactions)
        previous_counts = self._telegram_reaction_counts.get(key)
        self._telegram_reaction_counts[key] = current_counts

        if previous_counts is None:
            if not current_counts:
                return None
            best_emoji, _ = max(current_counts.items(), key=lambda item: item[1])
            return best_emoji

        positive_deltas: list[tuple[int, str]] = []
        for emoji, count in current_counts.items():
            delta = count - previous_counts.get(emoji, 0)
            if delta > 0:
                positive_deltas.append((delta, emoji))

        if not positive_deltas:
            return None

        positive_deltas.sort(reverse=True)
        return positive_deltas[0][1]

    def _record_telegram_reaction_write(self, chat_id: int, message_id: int, emoji: str) -> None:
        now = time.monotonic()
        self._prune_recent_telegram_reaction_writes(now)
        self._recent_telegram_reaction_writes[(chat_id, message_id, emoji)] = now

    def _was_recent_telegram_reaction_write(self, chat_id: int, message_id: int, emoji: str) -> bool:
        now = time.monotonic()
        self._prune_recent_telegram_reaction_writes(now)
        return (chat_id, message_id, emoji) in self._recent_telegram_reaction_writes

    def _prune_recent_telegram_reaction_writes(self, now: float) -> None:
        cutoff = now - TELEGRAM_REACTION_WRITE_DEDUPE_TTL_SECONDS
        stale_keys = [
            key for key, written_at in self._recent_telegram_reaction_writes.items() if written_at < cutoff
        ]
        for key in stale_keys:
            self._recent_telegram_reaction_writes.pop(key, None)

    async def _dispatch_telegram_message(self, event: TelegramMessageEvent) -> None:
        context = self._plugin_context()

        for loaded_plugin in self.plugins:
            try:
                actions = await loaded_plugin.instance.on_telegram_message(event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling telegram message", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    async def _dispatch_telegram_reaction(self, event: TelegramReactionEvent) -> None:
        context = self._plugin_context()

        for loaded_plugin in self.plugins:
            try:
                actions = await loaded_plugin.instance.on_telegram_reaction(event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling telegram reaction", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    def _on_meshtastic_packet(self, packet: dict[str, Any]) -> None:
        if self.loop is None:
            return

        packet_id = _extract_optional_int(packet.get("id"))
        if packet_id is not None and self._is_duplicate_meshtastic_packet_id(packet_id):
            return

        reaction_event = self._build_meshtastic_reaction_event(packet)
        if reaction_event is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch_meshtastic_reaction(reaction_event),
                self.loop,
            )
            future.add_done_callback(_log_future_exception)
            return

        message_event = self._build_meshtastic_event(packet)
        if message_event is None:
            return

        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_meshtastic_message(message_event),
            self.loop,
        )
        future.add_done_callback(_log_future_exception)

    def _is_duplicate_meshtastic_packet_id(self, packet_id: int) -> bool:
        now = time.monotonic()
        expiry_cutoff = now - MESHTASTIC_PACKET_ID_DEDUPE_TTL_SECONDS

        stale_packet_ids = [
            seen_packet_id
            for seen_packet_id, seen_time in self._seen_meshtastic_packet_ids.items()
            if seen_time < expiry_cutoff
        ]
        for stale_packet_id in stale_packet_ids:
            self._seen_meshtastic_packet_ids.pop(stale_packet_id, None)

        if packet_id in self._seen_meshtastic_packet_ids:
            return True

        self._seen_meshtastic_packet_ids[packet_id] = now
        return False

    def _build_meshtastic_event(self, packet: dict[str, Any]) -> Optional[MeshtasticTextEvent]:
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return None

        if not _is_text_message_portnum(decoded.get("portnum")):
            return None
        if _extract_reaction_emoji(decoded) is not None:
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

        from_num = _extract_optional_int(packet.get("from"))
        to_num = _extract_optional_int(packet.get("to"))

        from_id = _normalize_node_id(packet.get("fromId"), fallback_num=from_num)
        to_id = _normalize_node_id(packet.get("toId"), fallback_num=to_num)

        try:
            channel_index = int(packet.get("channel", 0))
        except (TypeError, ValueError):
            channel_index = 0

        packet_id = _extract_optional_int(packet.get("id"))

        reply_id = _extract_optional_int(decoded.get("replyId"))
        if reply_id is None:
            reply_id = _extract_optional_int(decoded.get("reply_id"))

        sender_label = self.meshtastic.resolve_sender_label(from_id, from_num=from_num)

        return MeshtasticTextEvent(
            from_id=from_id,
            to_id=to_id,
            packet_id=packet_id,
            reply_id=reply_id,
            channel_index=channel_index,
            text=text,
            sender_label=sender_label,
            raw_packet=packet,
        )

    def _build_meshtastic_reaction_event(self, packet: dict[str, Any]) -> Optional[MeshtasticReactionEvent]:
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return None
        if not _is_text_message_portnum(decoded.get("portnum")):
            return None

        emoji = _extract_reaction_emoji(decoded)
        if emoji is None:
            return None

        target_packet_id = _extract_optional_int(decoded.get("replyId"))
        if target_packet_id is None:
            target_packet_id = _extract_optional_int(decoded.get("reply_id"))
        if target_packet_id is None:
            target_packet_id = _extract_optional_int(packet.get("replyId"))
        if target_packet_id is None:
            target_packet_id = _extract_optional_int(packet.get("reply_id"))
        if target_packet_id is None:
            return None

        from_num = _extract_optional_int(packet.get("from"))
        to_num = _extract_optional_int(packet.get("to"))
        from_id = _normalize_node_id(packet.get("fromId"), fallback_num=from_num)
        to_id = _normalize_node_id(packet.get("toId"), fallback_num=to_num)

        try:
            channel_index = int(packet.get("channel", 0))
        except (TypeError, ValueError):
            channel_index = 0

        packet_id = _extract_optional_int(packet.get("id"))
        sender_label = self.meshtastic.resolve_sender_label(from_id, from_num=from_num)

        return MeshtasticReactionEvent(
            from_id=from_id,
            to_id=to_id,
            packet_id=packet_id,
            target_packet_id=target_packet_id,
            channel_index=channel_index,
            emoji=emoji,
            sender_label=sender_label,
            raw_packet=packet,
        )

    async def _dispatch_meshtastic_message(self, event: MeshtasticTextEvent) -> None:
        context = self._plugin_context()

        for loaded_plugin in self.plugins:
            try:
                actions = await loaded_plugin.instance.on_meshtastic_message(event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling meshtastic message", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    async def _dispatch_meshtastic_reaction(self, event: MeshtasticReactionEvent) -> None:
        context = self._plugin_context()

        for loaded_plugin in self.plugins:
            try:
                actions = await loaded_plugin.instance.on_meshtastic_reaction(event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling meshtastic reaction", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    async def _execute_actions(self, actions: list[PluginAction], plugin_name: str) -> None:
        aborted_sequences: set[str] = set()
        for action in actions:
            if isinstance(action, SendMeshtasticAction):
                sequence_id = action.sequence_id
                if (
                    sequence_id
                    and action.abort_on_failure
                    and sequence_id in aborted_sequences
                ):
                    LOGGER.warning(
                        "Skipping Meshtastic chunk due to prior sequence failure: "
                        "sequence=%s chunk=%s/%s plugin=%s",
                        sequence_id,
                        action.sequence_index,
                        action.sequence_total,
                        plugin_name,
                    )
                    continue

            try:
                if isinstance(action, SendTelegramAction):
                    sent_message = await self._execute_send_telegram(action)
                    self._register_reply_link_after_telegram_send(action, sent_message)
                elif isinstance(action, SendTelegramReactionAction):
                    await self._execute_send_telegram_reaction(action)
                elif isinstance(action, SendMeshtasticAction):
                    sent_packet = await self._execute_send_meshtastic(action)
                    self._register_reply_link_after_meshtastic_send(action, sent_packet)
                elif isinstance(action, SendMeshtasticReactionAction):
                    await self._execute_send_meshtastic_reaction(action)
                else:
                    LOGGER.warning("Plugin %s returned unknown action type: %s", plugin_name, type(action))
            except Exception:
                if (
                    isinstance(action, SendMeshtasticAction)
                    and action.sequence_id
                    and action.abort_on_failure
                ):
                    aborted_sequences.add(action.sequence_id)
                LOGGER.exception("Plugin %s failed executing action %s", plugin_name, type(action).__name__)

    async def _execute_send_telegram(self, action: SendTelegramAction):
        if self.bot_app is None:
            raise RuntimeError("Telegram bot app is not initialized")

        return await self.bot_app.bot.send_message(
            chat_id=action.chat_id,
            text=action.text,
            reply_to_message_id=action.reply_to_message_id,
        )

    async def _execute_send_telegram_reaction(self, action: SendTelegramReactionAction) -> None:
        if self.bot_app is None:
            raise RuntimeError("Telegram bot app is not initialized")

        candidates = _build_telegram_reaction_candidates(action.emoji)
        for index, emoji in enumerate(candidates):
            try:
                await self.bot_app.bot.set_message_reaction(
                    chat_id=action.chat_id,
                    message_id=action.message_id,
                    reaction=[ReactionTypeEmoji(emoji)],
                    is_big=action.is_big,
                )
                self._record_telegram_reaction_write(action.chat_id, action.message_id, emoji)
                if emoji != action.emoji:
                    LOGGER.info(
                        "Applied Telegram reaction fallback emoji '%s' for Meshtastic emoji '%s'",
                        emoji,
                        action.emoji,
                    )
                return
            except BadRequest as exc:
                error_text = str(exc).strip()
                if TELEGRAM_REACTION_INVALID_ERROR_TOKEN in error_text.casefold():
                    if index + 1 < len(candidates):
                        LOGGER.warning(
                            "Telegram rejected reaction '%s' as invalid; trying fallback '%s'",
                            emoji,
                            candidates[index + 1],
                        )
                        continue

                    LOGGER.warning(
                        "Telegram rejected all reaction candidates for '%s'; dropping reaction sync",
                        action.emoji,
                    )
                    return
                raise

    async def _execute_send_meshtastic(self, action: SendMeshtasticAction):
        async with self._meshtastic_send_lock:
            if action.delay_ms > 0:
                await asyncio.sleep(action.delay_ms / 1000)

            max_attempts = max(1, action.retry_max_attempts)
            retry_delay_seconds = max(0, action.retry_initial_delay_ms) / 1000
            backoff_factor = max(1.0, action.retry_backoff_factor)

            for attempt in range(1, max_attempts + 1):
                try:
                    if not self.meshtastic.is_connected:
                        if max_attempts == 1:
                            LOGGER.warning("Meshtastic is not connected yet; dropping outbound message")
                            return None
                        raise RuntimeError("Meshtastic is not connected yet")

                    sent_packet = self.meshtastic.send_text(action)
                    packet_id = _extract_meshtastic_packet_id(sent_packet)
                    if action.require_packet_id and packet_id is None:
                        raise RuntimeError("Meshtastic send returned no packet id")
                    if action.sequence_id is not None:
                        LOGGER.info(
                            "Meshtastic chunk sent: sequence=%s chunk=%s/%s packet_id=%s bytes=%s",
                            action.sequence_id,
                            action.sequence_index,
                            action.sequence_total,
                            packet_id,
                            len(action.text.encode("utf-8")),
                        )
                    return sent_packet
                except Exception as exc:
                    if _is_connection_error(exc):
                        self.meshtastic.invalidate_connection()

                    if attempt >= max_attempts:
                        LOGGER.error(
                            "Meshtastic send exhausted retries: sequence=%s chunk=%s/%s "
                            "attempts=%s abort_on_failure=%s",
                            action.sequence_id,
                            action.sequence_index,
                            action.sequence_total,
                            max_attempts,
                            action.abort_on_failure,
                            exc_info=True,
                        )
                        raise

                    LOGGER.warning(
                        "Meshtastic send failed; retrying in %.2fs "
                        "(attempt %s/%s, sequence=%s, chunk=%s/%s)",
                        retry_delay_seconds,
                        attempt + 1,
                        max_attempts,
                        action.sequence_id,
                        action.sequence_index,
                        action.sequence_total,
                    )
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds *= backoff_factor

            return None

    async def _execute_send_meshtastic_reaction(self, action: SendMeshtasticReactionAction):
        async with self._meshtastic_send_lock:
            max_attempts = max(1, action.retry_max_attempts)
            retry_delay_seconds = max(0, action.retry_initial_delay_ms) / 1000
            backoff_factor = max(1.0, action.retry_backoff_factor)

            for attempt in range(1, max_attempts + 1):
                try:
                    if not self.meshtastic.is_connected:
                        if max_attempts == 1:
                            LOGGER.warning("Meshtastic is not connected yet; dropping outbound reaction")
                            return None
                        raise RuntimeError("Meshtastic is not connected yet")

                    return self.meshtastic.send_reaction(action)
                except Exception as exc:
                    if _is_connection_error(exc):
                        self.meshtastic.invalidate_connection()

                    if attempt >= max_attempts:
                        LOGGER.error(
                            "Meshtastic reaction send exhausted retries: "
                            "attempts=%s chat-target=%s packet-target=%s",
                            max_attempts,
                            action.destination_id,
                            action.target_packet_id,
                            exc_info=True,
                        )
                        raise

                    LOGGER.warning(
                        "Meshtastic reaction send failed; retrying in %.2fs "
                        "(attempt %s/%s target_packet=%s)",
                        retry_delay_seconds,
                        attempt + 1,
                        max_attempts,
                        action.target_packet_id,
                    )
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds *= backoff_factor

            return None

    def _register_reply_link_after_telegram_send(
        self,
        action: SendTelegramAction,
        sent_message: Any,
    ) -> None:
        source_packet_id = action.bridge_source_meshtastic_packet_id
        if source_packet_id is None:
            return

        telegram_message_id = _extract_telegram_message_id(sent_message)
        if telegram_message_id is None:
            return

        self.reply_links.link_meshtastic_to_telegram(
            source_packet_id,
            action.chat_id,
            telegram_message_id,
        )
        self.reply_links.link_telegram_to_meshtastic(
            action.chat_id,
            telegram_message_id,
            source_packet_id,
        )

    def _register_reply_link_after_meshtastic_send(
        self,
        action: SendMeshtasticAction,
        sent_packet: Any,
    ) -> None:
        source_chat_id = action.bridge_source_telegram_chat_id
        source_message_id = action.bridge_source_telegram_message_id
        if source_chat_id is None or source_message_id is None:
            return

        meshtastic_packet_id = _extract_meshtastic_packet_id(sent_packet)
        if meshtastic_packet_id is None:
            return

        self.reply_links.link_meshtastic_to_telegram(
            meshtastic_packet_id,
            source_chat_id,
            source_message_id,
        )

        if action.bridge_canonical_for_telegram_message:
            self.reply_links.link_telegram_to_meshtastic(
                source_chat_id,
                source_message_id,
                meshtastic_packet_id,
            )

    def run(self) -> None:
        self.bot_app = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .build()
        )

        self.bot_app.add_handler(
            MessageHandler(filters.ALL & ~filters.COMMAND, self._handle_telegram_message)
        )
        self.bot_app.add_handler(
            MessageReactionHandler(
                self._handle_telegram_reaction,
                message_reaction_types=MessageReactionHandler.MESSAGE_REACTION,
            )
        )

        LOGGER.info("Starting Meshgram polling loop")
        try:
            self.bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
        finally:
            if self._mesh_connect_task is not None:
                self._mesh_connect_task.cancel()
            self.meshtastic.close()

def _message_has_media(message: Message) -> bool:
    media_fields = (
        "animation",
        "audio",
        "document",
        "photo",
        "sticker",
        "video",
        "video_note",
        "voice",
    )

    for field in media_fields:
        value = getattr(message, field, None)
        if value:
            return True
    return False


def _extract_telegram_reply_to_message_id(message: Message) -> Optional[int]:
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to is None:
        return None

    value = getattr(reply_to, "message_id", None)
    if isinstance(value, int):
        return value
    return None


def _extract_first_unicode_reaction_emoji(
    reaction_update: MessageReactionUpdated,
) -> Optional[str]:
    new_reaction = getattr(reaction_update, "new_reaction", None)
    if not new_reaction:
        return None

    for reaction in new_reaction:
        if isinstance(reaction, ReactionTypeEmoji):
            emoji = getattr(reaction, "emoji", None)
            if isinstance(emoji, str) and emoji:
                return emoji

    return None


def _build_telegram_reaction_candidates(emoji: str) -> list[str]:
    text = emoji.strip()
    if not text:
        return [DEFAULT_TELEGRAM_REACTION_FALLBACK_EMOJI]

    candidates: list[str] = []
    for value in (
        text,
        _telegram_reaction_alias(text),
        f"{text}\ufe0f",
        text.replace("\ufe0f", ""),
        DEFAULT_TELEGRAM_REACTION_FALLBACK_EMOJI,
    ):
        normalized = value.strip()
        if not normalized:
            continue
        if normalized not in candidates:
            candidates.append(normalized)

    return candidates


def _telegram_reaction_alias(emoji: str) -> str:
    aliases = {
        "❤": "❤️",
        "♥": "❤️",
        "☻": "🙂",
        "☺": "🙂",
    }
    return aliases.get(emoji, emoji)


def _extract_unicode_emoji_counts(reactions: Sequence[ReactionCount]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reaction_count in reactions:
        reaction = getattr(reaction_count, "type", None)
        if not isinstance(reaction, ReactionTypeEmoji):
            continue

        emoji = getattr(reaction, "emoji", None)
        if not isinstance(emoji, str) or not emoji:
            continue

        total_count = _extract_optional_int(getattr(reaction_count, "total_count", None))
        if total_count is None:
            continue

        counts[emoji] = total_count
    return counts


def _extract_reaction_emoji(decoded: dict[str, Any]) -> Optional[str]:
    raw_emoji = decoded.get("emoji")

    emoji_codepoint = _extract_optional_int(raw_emoji)
    if emoji_codepoint is not None:
        if emoji_codepoint <= 0:
            return None
        with contextlib.suppress(ValueError):
            return chr(emoji_codepoint)
        return None

    if isinstance(raw_emoji, str):
        text = raw_emoji.strip()
        if text:
            return text[0]

    if isinstance(raw_emoji, bytes):
        decoded_emoji = raw_emoji.decode("utf-8", errors="ignore").strip()
        if decoded_emoji:
            return decoded_emoji[0]

    return None


def _is_text_message_portnum(portnum: Any) -> bool:
    if portnum == "TEXT_MESSAGE_APP":
        return True

    text_portnum_value: Optional[int] = None
    if portnums_pb2 is not None:
        text_portnum_value = int(portnums_pb2.PortNum.TEXT_MESSAGE_APP)
    else:
        # Meshtastic TEXT_MESSAGE_APP is historically enum value 1.
        text_portnum_value = 1

    if isinstance(portnum, int):
        return portnum == text_portnum_value

    if isinstance(portnum, str):
        normalized = portnum.strip()
        if not normalized:
            return False
        if normalized == "TEXT_MESSAGE_APP":
            return True
        maybe_int = _extract_optional_int(normalized)
        return maybe_int == text_portnum_value

    return False


def _is_connection_error(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError, OSError, EOFError))


def _extract_optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        with contextlib.suppress(ValueError):
            return int(stripped)
        with contextlib.suppress(ValueError):
            return int(stripped, 0)
    return None


def _node_num_to_id(node_num: int) -> str:
    return f"!{node_num & 0xFFFFFFFF:08x}"


def _normalize_node_id(value: Any, fallback_num: Optional[int] = None) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            if text.startswith("!"):
                text = text[1:]
            if text.lower().startswith("0x"):
                text = text[2:]

            if text.isdigit():
                if len(text) == 8:
                    return f"!{text.lower()}"
                return _node_num_to_id(int(text))

            if text and all(char in "0123456789abcdefABCDEF" for char in text):
                return f"!{text.lower()}"

            return value.strip()

    if fallback_num is not None:
        return _node_num_to_id(fallback_num)

    return None


def _extract_telegram_message_id(sent_message: Any) -> Optional[int]:
    if sent_message is None:
        return None

    value = getattr(sent_message, "message_id", None)
    if isinstance(value, int):
        return value
    return None


def _extract_meshtastic_packet_id(sent_packet: Any) -> Optional[int]:
    if sent_packet is None:
        return None

    if isinstance(sent_packet, dict):
        return _extract_optional_int(sent_packet.get("id"))

    value = getattr(sent_packet, "id", None)
    return _extract_optional_int(value)


def _get_bridge_reply_ttl_hours(settings: MeshgramSettings) -> int:
    default_ttl_hours = 24

    for plugin in settings.plugins:
        if plugin.name != "bridge":
            continue

        value = plugin.settings.get("reply_link_ttl_hours", default_ttl_hours)
        ttl_hours = _extract_optional_int(value)
        if ttl_hours is None or ttl_hours <= 0:
            return default_ttl_hours
        return ttl_hours

    return default_ttl_hours


def _log_future_exception(future: asyncio.Future[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exception = future.exception()
        if exception:
            LOGGER.error(
                "Meshtastic dispatch failed: %s",
                exception,
                exc_info=(type(exception), exception, exception.__traceback__),
            )


def main() -> None:
    settings = load_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )

    app = MeshgramApp(settings)
    app.run()

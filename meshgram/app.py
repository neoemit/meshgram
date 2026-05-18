from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional, Sequence

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

from ._mesh_helpers import (
    extract_optional_int,
    extract_reaction_emoji_from_value,
    is_broadcast_destination,
    is_emoji_modifier,
    sanitize_reaction_emoji_text,
)
from .config import MESHTASTIC_BACKEND, MeshgramSettings, load_settings
from .plugin import LoadedPlugin, load_plugins
from .reply_links import ReplyLinkRegistry
from .transport import MeshTransport, create_transport
from .types import (
    MeshPacketRef,
    MeshReactionEvent,
    MeshTextEvent,
    PluginAction,
    PluginContext,
    SendMeshAction,
    SendMeshReactionAction,
    SendTelegramAction,
    SendTelegramReactionAction,
    TelegramMessageEvent,
    TelegramReactionEvent,
    # Backward-compatible aliases (kept so external imports stay valid):
    MeshtasticReactionEvent,
    MeshtasticTextEvent,
    SendMeshtasticAction,
    SendMeshtasticReactionAction,
)

LOGGER = logging.getLogger(__name__)
MESHTASTIC_PACKET_ID_DEDUPE_TTL_SECONDS = 120.0
TELEGRAM_REACTION_WRITE_DEDUPE_TTL_SECONDS = 12.0
DEFAULT_TELEGRAM_REACTION_FALLBACK_EMOJI = "👍"
TELEGRAM_REACTION_INVALID_ERROR_TOKEN = "reaction_invalid"


class MeshgramApp:
    def __init__(self, settings: MeshgramSettings):
        self.settings = settings
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bot_app: Optional[Application] = None
        self.mesh: MeshTransport = create_transport(settings)
        self.plugins: list[LoadedPlugin] = load_plugins(settings.plugins)
        self.reply_links = ReplyLinkRegistry(
            ttl_hours=_get_bridge_reply_ttl_hours(settings),
        )
        self._mesh_connect_task: Optional[asyncio.Task[None]] = None
        self._seen_meshtastic_packet_ids: dict[MeshPacketRef, float] = {}
        self._meshtastic_send_lock = asyncio.Lock()
        self._telegram_reaction_counts: dict[tuple[int, int], dict[str, int]] = {}
        self._recent_telegram_reaction_writes: dict[tuple[int, int, str], float] = {}

    # Backward-compatible alias — tests and older callsites access ``app.meshtastic``.
    @property
    def meshtastic(self) -> MeshTransport:
        return self.mesh

    @meshtastic.setter
    def meshtastic(self, value: MeshTransport) -> None:
        self.mesh = value

    async def _post_init(self, app: Application) -> None:
        self.loop = asyncio.get_running_loop()
        self._mesh_connect_task = asyncio.create_task(self._ensure_mesh_connected())

        await self._dispatch_startup()
        LOGGER.info("Meshgram runtime initialized (backend=%s)", self.mesh.backend_name)

    async def _ensure_mesh_connected(self) -> None:
        retry_delay_seconds = 5
        healthy_poll_seconds = 2

        while True:
            if self.mesh.is_connected:
                await asyncio.sleep(healthy_poll_seconds)
                continue

            try:
                loop = asyncio.get_running_loop()
                await self.mesh.connect(loop, self._on_mesh_text, self._on_mesh_reaction)
                LOGGER.info("Mesh connection established (backend=%s)", self.mesh.backend_name)
            except Exception as exc:
                self.mesh.invalidate_connection()
                LOGGER.warning(
                    "Mesh connection failed (backend=%s, error=%s). Retrying in %ss.",
                    self.mesh.backend_name,
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
        self.mesh.refresh_local_node_id()
        return PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=self.mesh.payload_limit,
            local_node_id=self.mesh.local_node_id,
            reply_links=self.reply_links,
        )

    # --- Telegram handlers --------------------------------------------------

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

    # --- Mesh inbound -------------------------------------------------------

    async def _on_mesh_text(self, event: MeshTextEvent) -> None:
        if event.packet_id is not None and self._is_duplicate_meshtastic_packet_id(event.packet_id):
            return
        await self._dispatch_mesh_message(event)

    async def _on_mesh_reaction(self, event: MeshReactionEvent) -> None:
        if event.packet_id is not None and self._is_duplicate_meshtastic_packet_id(event.packet_id):
            return
        await self._dispatch_mesh_reaction(event)

    def _is_duplicate_meshtastic_packet_id(self, packet_id: MeshPacketRef) -> bool:
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

    async def _dispatch_mesh_message(self, event: MeshTextEvent) -> None:
        context = self._plugin_context()

        for loaded_plugin in self.plugins:
            try:
                actions = await _invoke_plugin_mesh_message(loaded_plugin.instance, event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling mesh message", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    async def _dispatch_mesh_reaction(self, event: MeshReactionEvent) -> None:
        context = self._plugin_context()

        for loaded_plugin in self.plugins:
            try:
                actions = await _invoke_plugin_mesh_reaction(loaded_plugin.instance, event, context)
            except Exception:
                LOGGER.exception("Plugin %s failed handling mesh reaction", loaded_plugin.name)
                continue

            await self._execute_actions(actions, loaded_plugin.name)

    # Backward-compatible method aliases (used by existing tests):
    async def _dispatch_meshtastic_message(self, event: MeshTextEvent) -> None:
        await self._dispatch_mesh_message(event)

    async def _dispatch_meshtastic_reaction(self, event: MeshReactionEvent) -> None:
        await self._dispatch_mesh_reaction(event)

    # --- Action execution ---------------------------------------------------

    async def _execute_actions(self, actions: list[PluginAction], plugin_name: str) -> None:
        aborted_sequences: set[str] = set()
        for action in actions:
            if isinstance(action, SendMeshAction):
                sequence_id = action.sequence_id
                if (
                    sequence_id
                    and action.abort_on_failure
                    and sequence_id in aborted_sequences
                ):
                    LOGGER.warning(
                        "Skipping mesh chunk due to prior sequence failure: "
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
                elif isinstance(action, SendMeshAction):
                    sent_packet = await self._execute_send_mesh(action)
                    self._register_reply_link_after_mesh_send(action, sent_packet)
                elif isinstance(action, SendMeshReactionAction):
                    await self._execute_send_mesh_reaction(action)
                else:
                    LOGGER.warning("Plugin %s returned unknown action type: %s", plugin_name, type(action))
            except Exception:
                if (
                    isinstance(action, SendMeshAction)
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
                        "Applied Telegram reaction fallback emoji '%s' for mesh emoji '%s'",
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

    async def _execute_send_mesh(self, action: SendMeshAction):
        async with self._meshtastic_send_lock:
            if action.delay_ms > 0:
                await asyncio.sleep(action.delay_ms / 1000)

            max_attempts = max(1, action.retry_max_attempts)
            retry_delay_seconds = max(0, action.retry_initial_delay_ms) / 1000
            backoff_factor = max(1.0, action.retry_backoff_factor)

            for attempt in range(1, max_attempts + 1):
                try:
                    if not self.mesh.is_connected:
                        if max_attempts == 1:
                            LOGGER.warning("Mesh transport is not connected yet; dropping outbound message")
                            return None
                        raise RuntimeError("Mesh transport is not connected yet")

                    sent_packet = await self.mesh.asend_text(action)
                    packet_id = MeshTransport.extract_packet_id(sent_packet)
                    if action.require_packet_id and packet_id is None:
                        raise RuntimeError("Mesh send returned no packet id")
                    should_wait_for_ack = (
                        action.wait_for_ack
                        and action.want_ack
                        and not is_broadcast_destination(action.destination_id)
                    )
                    if should_wait_for_ack and self.mesh.supports_wait_for_ack:
                        ack_timeout_seconds = max(1.0, action.ack_timeout_ms / 1000)
                        await asyncio.wait_for(
                            self.mesh.wait_for_ack(),
                            timeout=ack_timeout_seconds,
                        )
                        if action.sequence_id is not None:
                            LOGGER.info(
                                "Mesh ACK received: sequence=%s chunk=%s/%s packet_id=%s",
                                action.sequence_id,
                                action.sequence_index,
                                action.sequence_total,
                                packet_id,
                            )
                    elif should_wait_for_ack and not self.mesh.supports_wait_for_ack:
                        LOGGER.debug(
                            "Mesh transport %s does not expose a generic wait_for_ack; "
                            "trusting per-send ACK handling.",
                            self.mesh.backend_name,
                        )
                    if action.sequence_id is not None:
                        LOGGER.info(
                            "Mesh chunk sent: sequence=%s chunk=%s/%s packet_id=%s bytes=%s",
                            action.sequence_id,
                            action.sequence_index,
                            action.sequence_total,
                            packet_id,
                            len(action.text.encode("utf-8")),
                        )
                    return sent_packet
                except Exception as exc:
                    if _is_connection_error(exc):
                        self.mesh.invalidate_connection()

                    if attempt >= max_attempts:
                        LOGGER.error(
                            "Mesh send exhausted retries: sequence=%s chunk=%s/%s "
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
                        "Mesh send failed; retrying in %.2fs "
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

    async def _execute_send_mesh_reaction(self, action: SendMeshReactionAction):
        if not self.mesh.supports_reactions:
            LOGGER.debug(
                "Mesh backend %s does not support reactions; dropping emoji=%s target=%s",
                self.mesh.backend_name,
                action.emoji,
                action.target_packet_id,
            )
            return None

        async with self._meshtastic_send_lock:
            max_attempts = max(1, action.retry_max_attempts)
            retry_delay_seconds = max(0, action.retry_initial_delay_ms) / 1000
            backoff_factor = max(1.0, action.retry_backoff_factor)

            for attempt in range(1, max_attempts + 1):
                try:
                    if not self.mesh.is_connected:
                        if max_attempts == 1:
                            LOGGER.warning("Mesh transport is not connected yet; dropping outbound reaction")
                            return None
                        raise RuntimeError("Mesh transport is not connected yet")

                    return await self.mesh.asend_reaction(action)
                except Exception as exc:
                    if _is_connection_error(exc):
                        self.mesh.invalidate_connection()

                    if attempt >= max_attempts:
                        LOGGER.error(
                            "Mesh reaction send exhausted retries: "
                            "attempts=%s chat-target=%s packet-target=%s",
                            max_attempts,
                            action.destination_id,
                            action.target_packet_id,
                            exc_info=True,
                        )
                        raise

                    LOGGER.warning(
                        "Mesh reaction send failed; retrying in %.2fs "
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

    # Backward-compatible method aliases (used by existing tests):
    async def _execute_send_meshtastic(self, action: SendMeshAction):
        return await self._execute_send_mesh(action)

    async def _execute_send_meshtastic_reaction(self, action: SendMeshReactionAction):
        return await self._execute_send_mesh_reaction(action)

    # --- Reply-link bookkeeping --------------------------------------------

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

    def _register_reply_link_after_mesh_send(
        self,
        action: SendMeshAction,
        sent_packet: Any,
    ) -> None:
        source_chat_id = action.bridge_source_telegram_chat_id
        source_message_id = action.bridge_source_telegram_message_id
        if source_chat_id is None or source_message_id is None:
            return

        meshtastic_packet_id = MeshTransport.extract_packet_id(sent_packet)
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

    # Backward-compatible alias:
    def _register_reply_link_after_meshtastic_send(self, action, sent_packet):  # type: ignore[override]
        self._register_reply_link_after_mesh_send(action, sent_packet)

    # --- Test-facing helpers (event building) -------------------------------
    # The current sender-resolution tests still call into these helpers via the
    # app object. They delegate to the meshtastic transport's builders.

    def _build_meshtastic_event(self, packet: dict[str, Any]) -> Optional[MeshTextEvent]:
        if self.mesh.backend_name != MESHTASTIC_BACKEND:
            return None
        from .transport.meshtastic import MeshtasticTransport

        assert isinstance(self.mesh, MeshtasticTransport)
        return self.mesh._build_text_event(packet)

    def _build_meshtastic_reaction_event(self, packet: dict[str, Any]) -> Optional[MeshReactionEvent]:
        if self.mesh.backend_name != MESHTASTIC_BACKEND:
            return None
        from .transport.meshtastic import MeshtasticTransport

        assert isinstance(self.mesh, MeshtasticTransport)
        return self.mesh._build_reaction_event(packet)

    # --- Lifecycle ----------------------------------------------------------

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
                message_reaction_types=_message_reaction_handler_types(),
            )
        )

        LOGGER.info("Starting Meshgram polling loop (backend=%s)", self.mesh.backend_name)
        try:
            self.bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
        finally:
            if self._mesh_connect_task is not None:
                self._mesh_connect_task.cancel()
            self.mesh.close()


# ---------------------------------------------------------------------------
# Plugin dispatch shims — try the new mesh_* hook, fall back to legacy meshtastic_*

async def _invoke_plugin_mesh_message(
    plugin_instance: Any,
    event: MeshTextEvent,
    context: PluginContext,
) -> list[PluginAction]:
    hook = getattr(plugin_instance, "on_mesh_message", None)
    if callable(hook):
        return await hook(event, context)
    legacy = getattr(plugin_instance, "on_meshtastic_message", None)
    if callable(legacy):
        return await legacy(event, context)
    return []


async def _invoke_plugin_mesh_reaction(
    plugin_instance: Any,
    event: MeshReactionEvent,
    context: PluginContext,
) -> list[PluginAction]:
    hook = getattr(plugin_instance, "on_mesh_reaction", None)
    if callable(hook):
        return await hook(event, context)
    legacy = getattr(plugin_instance, "on_meshtastic_reaction", None)
    if callable(legacy):
        return await legacy(event, context)
    return []


# ---------------------------------------------------------------------------
# Telegram-side helpers

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
    text = sanitize_reaction_emoji_text(emoji)
    if not text:
        return [DEFAULT_TELEGRAM_REACTION_FALLBACK_EMOJI]

    candidates: list[str] = []
    for value in (
        text,
        _telegram_reaction_alias(text),
        f"{text}️",
        text.replace("️", ""),
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

        total_count = extract_optional_int(getattr(reaction_count, "total_count", None))
        if total_count is None:
            continue

        counts[emoji] = total_count
    return counts


def _is_connection_error(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError, OSError, EOFError))


def _message_reaction_handler_types() -> int:
    types = getattr(MessageReactionHandler, "MESSAGE_REACTION", 0)
    for attribute in (
        "MESSAGE_REACTION_UPDATED",
        "MESSAGE_REACTION_COUNT",
        "MESSAGE_REACTION_COUNT_UPDATED",
    ):
        value = getattr(MessageReactionHandler, attribute, None)
        if isinstance(value, int):
            types |= value
    if not isinstance(types, int) or types == 0:
        return MessageReactionHandler.MESSAGE_REACTION
    return types


def _extract_telegram_message_id(sent_message: Any) -> Optional[int]:
    if sent_message is None:
        return None

    value = getattr(sent_message, "message_id", None)
    if isinstance(value, int):
        return value
    return None


def _get_bridge_reply_ttl_hours(settings: MeshgramSettings) -> int:
    default_ttl_hours = 24

    for plugin in settings.plugins:
        if plugin.name != "bridge":
            continue

        value = plugin.settings.get("reply_link_ttl_hours", default_ttl_hours)
        ttl_hours = extract_optional_int(value)
        if ttl_hours is None or ttl_hours <= 0:
            return default_ttl_hours
        return ttl_hours

    return default_ttl_hours


def main() -> None:
    settings = load_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )

    app = MeshgramApp(settings)
    app.run()


# Backward-compatible export so ``from meshgram.app import MeshtasticClient`` keeps working.
from .transport.meshtastic import MeshtasticClient  # noqa: E402,F401

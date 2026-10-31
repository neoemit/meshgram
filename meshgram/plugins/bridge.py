from __future__ import annotations

import logging
import uuid

from meshgram.plugin import CHANNEL_FORMAT, BasePlugin
from meshgram.text_utils import split_for_mesh, utf8_len
from meshgram.types import (
    MeshTextEvent,
    PluginAction,
    PluginContext,
    SendMeshAction,
    SendTelegramAction,
    TelegramMessageEvent,
    TelegramReactionEvent,
)

LOGGER = logging.getLogger(__name__)


class BridgePlugin(BasePlugin):
    name = "bridge"
    title = "Telegram bridge"
    description = (
        "Relays messages between the Telegram group and one radio channel. Long Telegram "
        "messages are split into numbered chunks that fit in a radio packet. Emoji reactions "
        "in Telegram go out as a short message quoting the message reacted to."
    )
    settings_schema = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "format": CHANNEL_FORMAT,
                "title": "Radio channel",
                "description": "The channel bridged to the Telegram group. Empty: meshcore.bridge_channel from config.yaml.",
            },
            "relay_reactions": {
                "type": "boolean",
                "default": True,
                "title": "Relay reactions",
                "description": (
                    "Send emoji reactions in Telegram to the radio channel as a message, e.g. "
                    "[Alice] reacted 👍 to \"[Bob] hello\". Telegram only tells the bot about "
                    "reactions when it's an admin of the group; reactions to messages Meshgram "
                    "didn't see since it started, and custom emoji, are left out."
                ),
            },
        },
    }
    MIN_CHUNK_DELAY_MS = 900
    DEFAULT_SAFE_MAX_CHUNK_BYTES = 160

    def _bridge_channel(self, context: PluginContext) -> int:
        configured_channel = self.settings.get("channel")
        if configured_channel is not None:
            try:
                return int(configured_channel)
            except (TypeError, ValueError):
                LOGGER.warning("bridge.settings.channel must be an integer; falling back to meshcore.bridge_channel")

        return context.settings.meshcore.bridge_channel

    def _payload_limit(self, context: PluginContext) -> int:
        chunking = context.settings.chunking
        # Channel messages are broadcasts: apply the broadcast size cap and pacing.
        payload_limit = context.mesh_payload_limit - max(0, chunking.payload_safety_margin_bytes)
        configured_max_chunk_bytes = chunking.max_chunk_bytes
        effective_max_chunk_bytes = (
            configured_max_chunk_bytes
            if configured_max_chunk_bytes > 0
            else self.DEFAULT_SAFE_MAX_CHUNK_BYTES
        )
        broadcast_cap = (
            chunking.broadcast_max_chunk_bytes
            if chunking.broadcast_max_chunk_bytes > 0
            else effective_max_chunk_bytes
        )
        effective_max_chunk_bytes = min(effective_max_chunk_bytes, broadcast_cap)
        return min(payload_limit, effective_max_chunk_bytes)

    def _with_sender_prefix(self, context: PluginContext, sender_display_name: str, text: str) -> str:
        template = context.settings.telegram.sender_prefix_template
        compact_display_name = _compact_display_name(sender_display_name)
        try:
            return template.format(display_name=compact_display_name, message=text)
        except (KeyError, ValueError):
            LOGGER.warning(
                "Invalid sender_prefix_template placeholders; expected display_name/message. Falling back."
            )
            return f"[{compact_display_name}] {text}"

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        bridge_channel = self._bridge_channel(context)
        if event.channel_index != bridge_channel:
            return []

        if context.local_node_id and event.from_id == context.local_node_id:
            return []

        text = event.text.strip()
        if not text:
            return []

        return [SendTelegramAction(chat_id=context.telegram_group_id, text=f"[{event.sender_label}] {text}")]

    async def on_telegram_message(
        self,
        event: TelegramMessageEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if event.chat_id != context.telegram_group_id:
            return []

        if event.is_from_bot:
            return []

        if not event.text:
            return []

        if event.text_source == "caption" and not context.settings.telegram.include_captions:
            return []

        text = event.text.strip()
        if not text:
            return []

        mesh_text = self._with_sender_prefix(context, event.sender_display_name, text)

        chunking = context.settings.chunking
        payload_limit = self._payload_limit(context)
        min_split_payload_limit = utf8_len(chunking.prefix_template.format(index=1, total=1)) + 1
        payload_limit = max(min_split_payload_limit, payload_limit)
        try:
            chunks = split_for_mesh(
                text=mesh_text,
                payload_limit=payload_limit,
                prefix_template=chunking.prefix_template,
                chunking_enabled=chunking.enabled,
            )
        except ValueError as exc:
            if "Chunk prefix leaves no space for payload" not in str(exc):
                raise

            LOGGER.warning(
                "Chunk payload safety margin is too aggressive for this message; "
                "falling back to full mesh payload limit"
            )
            chunks = split_for_mesh(
                text=mesh_text,
                payload_limit=context.mesh_payload_limit,
                prefix_template=chunking.prefix_template,
                chunking_enabled=chunking.enabled,
            )

        actions: list[PluginAction] = []
        bridge_channel = self._bridge_channel(context)
        is_chunked = len(chunks) > 1
        sequence_id = _chunk_sequence_id(event) if is_chunked else None
        configured_delay_ms = max(0, chunking.inter_chunk_delay_ms)
        effective_chunk_delay_ms = configured_delay_ms
        if is_chunked:
            effective_chunk_delay_ms = max(
                configured_delay_ms,
                self.MIN_CHUNK_DELAY_MS,
                max(0, chunking.broadcast_min_inter_chunk_delay_ms),
            )
            LOGGER.info(
                "Chunked Telegram message prepared: chat_id=%s message_id=%s sequence=%s chunks=%s payload_limit=%s delay_ms=%s",
                event.chat_id,
                event.message_id,
                sequence_id,
                len(chunks),
                payload_limit,
                effective_chunk_delay_ms,
            )
        for index, chunk in enumerate(chunks):
            delay_ms = effective_chunk_delay_ms if index > 0 else 0
            actions.append(
                SendMeshAction(
                    text=chunk,
                    channel_index=bridge_channel,
                    delay_ms=delay_ms,
                    retry_max_attempts=chunking.retry_max_attempts,
                    retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                    retry_backoff_factor=chunking.retry_backoff_factor,
                    sequence_id=sequence_id,
                    sequence_index=(index + 1) if is_chunked else None,
                    sequence_total=len(chunks) if is_chunked else None,
                    abort_on_failure=chunking.abort_on_chunk_failure if is_chunked else False,
                )
            )

        return actions

    async def on_telegram_reaction(
        self,
        event: TelegramReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if not self.settings.get("relay_reactions", True):
            return []

        if event.chat_id != context.telegram_group_id or event.is_from_bot or not event.emojis:
            return []

        # MeshCore has no reactions: a message names it, and the message reacted to,
        # which the reaction update itself doesn't carry. Without it, say nothing.
        quoted = " ".join((event.message_text or "").split())
        if not quoted:
            LOGGER.debug(
                "Reaction to unknown Telegram message %s not relayed: its text isn't known",
                event.message_id,
            )
            return []

        emojis = "".join(event.emojis)
        payload_limit = self._payload_limit(context)
        mesh_text = self._with_sender_prefix(
            context, event.sender_display_name, f'reacted {emojis} to "{quoted}"'
        )
        if utf8_len(mesh_text) > payload_limit:
            # One packet: shorten the quote rather than send a reaction in chunks.
            overflow = utf8_len(mesh_text) - payload_limit
            keep = utf8_len(quoted) - overflow - utf8_len(ELLIPSIS)
            shortened = _truncate_utf8(quoted, keep).rstrip()
            if not shortened:
                LOGGER.debug("Reaction to Telegram message %s doesn't fit in a radio packet", event.message_id)
                return []
            mesh_text = self._with_sender_prefix(
                context, event.sender_display_name, f'reacted {emojis} to "{shortened}{ELLIPSIS}"'
            )
            if utf8_len(mesh_text) > payload_limit:
                return []

        chunking = context.settings.chunking
        return [
            SendMeshAction(
                text=mesh_text,
                channel_index=self._bridge_channel(context),
                retry_max_attempts=chunking.retry_max_attempts,
                retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                retry_backoff_factor=chunking.retry_backoff_factor,
            )
        ]


ELLIPSIS = "…"


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _compact_display_name(sender_display_name: str) -> str:
    normalized = " ".join(sender_display_name.split())
    if not normalized:
        return sender_display_name
    return normalized.split(" ", 1)[0]


def _chunk_sequence_id(event: TelegramMessageEvent) -> str:
    random_suffix = uuid.uuid4().hex[:8]
    return f"tg-{event.chat_id}-{event.message_id}-{random_suffix}"

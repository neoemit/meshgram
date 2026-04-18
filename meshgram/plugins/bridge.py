from __future__ import annotations

import logging
import uuid

from meshgram.plugin import BasePlugin
from meshgram.text_utils import split_for_meshtastic, utf8_len
from meshgram.types import (
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


class BridgePlugin(BasePlugin):
    name = "bridge"
    DEFAULT_REPLY_MISSING_SUFFIX = "(reply target not found)"
    DEFAULT_REACTION_MISSING_NOTICE = "(reaction target not found)"
    DEFAULT_MESHTASTIC_WANT_ACK = True
    REPLY_ID_EXTRA_MARGIN_BYTES = 8
    MIN_CHUNK_DELAY_MS = 400

    def _bridge_channel(self, context: PluginContext) -> int:
        configured_channel = self.settings.get("channel")
        if configured_channel is None:
            return context.settings.meshtastic.bridge_channel

        try:
            return int(configured_channel)
        except (TypeError, ValueError):
            LOGGER.warning(
                "bridge.settings.channel must be an integer; falling back to meshtastic.bridge_channel"
            )
            return context.settings.meshtastic.bridge_channel

    async def on_meshtastic_message(
        self,
        event: MeshtasticTextEvent,
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

        telegram_reply_to_message_id = None
        if event.reply_id is not None and context.reply_links is not None:
            telegram_reply_to_message_id = context.reply_links.get_telegram_for_meshtastic(
                context.telegram_group_id,
                event.reply_id,
            )
            if telegram_reply_to_message_id is None and self._should_emit_missing_target_fallback():
                text = f"{text} {self._reply_missing_suffix()}".strip()

        return [
            SendTelegramAction(
                chat_id=context.telegram_group_id,
                text=f"[{event.sender_label}] {text}",
                reply_to_message_id=telegram_reply_to_message_id,
                bridge_source_meshtastic_packet_id=event.packet_id,
            )
        ]

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

        meshtastic_reply_id = None
        if event.reply_to_message_id is not None and context.reply_links is not None:
            meshtastic_reply_id = context.reply_links.get_meshtastic_for_telegram(
                event.chat_id,
                event.reply_to_message_id,
            )
            if meshtastic_reply_id is None and self._should_emit_missing_target_fallback():
                text = f"{text} {self._reply_missing_suffix()}".strip()

        template = context.settings.telegram.sender_prefix_template
        compact_display_name = _compact_display_name(event.sender_display_name)
        try:
            meshtastic_text = template.format(display_name=compact_display_name, message=text)
        except (KeyError, ValueError):
            LOGGER.warning(
                "Invalid sender_prefix_template placeholders; expected display_name/message. Falling back."
            )
            meshtastic_text = f"[{compact_display_name}] {text}"

        chunking = context.settings.chunking
        payload_limit = context.meshtastic_payload_limit - max(0, chunking.payload_safety_margin_bytes)
        if meshtastic_reply_id is not None:
            # reply_id adds protobuf bytes; reserve extra headroom to avoid edge-size drops.
            payload_limit -= self.REPLY_ID_EXTRA_MARGIN_BYTES
        min_split_payload_limit = utf8_len(chunking.prefix_template.format(index=1, total=1)) + 1
        payload_limit = max(min_split_payload_limit, payload_limit)
        try:
            chunks = split_for_meshtastic(
                text=meshtastic_text,
                payload_limit=payload_limit,
                prefix_template=chunking.prefix_template,
                chunking_enabled=chunking.enabled,
            )
        except ValueError as exc:
            if "Chunk prefix leaves no space for payload" not in str(exc):
                raise

            LOGGER.warning(
                "Chunk payload safety margin is too aggressive for this message; "
                "falling back to full Meshtastic payload limit"
            )
            chunks = split_for_meshtastic(
                text=meshtastic_text,
                payload_limit=context.meshtastic_payload_limit,
                prefix_template=chunking.prefix_template,
                chunking_enabled=chunking.enabled,
            )

        actions: list[PluginAction] = []
        bridge_channel = self._bridge_channel(context)
        is_chunked = len(chunks) > 1
        sequence_id = _chunk_sequence_id(event) if is_chunked else None
        want_ack = self._meshtastic_want_ack()
        configured_delay_ms = max(0, chunking.inter_chunk_delay_ms)
        effective_chunk_delay_ms = (
            max(configured_delay_ms, self.MIN_CHUNK_DELAY_MS)
            if is_chunked
            else configured_delay_ms
        )
        if is_chunked and configured_delay_ms < self.MIN_CHUNK_DELAY_MS:
            LOGGER.info(
                "Enforcing minimum inter-chunk delay for reliability: configured=%sms effective=%sms",
                configured_delay_ms,
                effective_chunk_delay_ms,
            )
        if is_chunked:
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
                SendMeshtasticAction(
                    text=chunk,
                    channel_index=bridge_channel,
                    reply_id=meshtastic_reply_id if index == 0 else None,
                    want_ack=want_ack,
                    delay_ms=delay_ms,
                    retry_max_attempts=chunking.retry_max_attempts,
                    retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                    retry_backoff_factor=chunking.retry_backoff_factor,
                    sequence_id=sequence_id,
                    sequence_index=(index + 1) if is_chunked else None,
                    sequence_total=len(chunks) if is_chunked else None,
                    abort_on_failure=chunking.abort_on_chunk_failure if is_chunked else False,
                    require_packet_id=True,
                    bridge_source_telegram_chat_id=event.chat_id,
                    bridge_source_telegram_message_id=event.message_id,
                    bridge_canonical_for_telegram_message=index == 0,
                )
            )

        return actions

    async def on_telegram_reaction(
        self,
        event: TelegramReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if not self._reactions_enabled():
            return []
        if event.chat_id != context.telegram_group_id:
            return []
        if event.is_from_bot:
            return []

        bridge_channel = self._bridge_channel(context)
        want_ack = self._meshtastic_want_ack()
        target_packet_id = None
        if context.reply_links is not None:
            target_packet_id = context.reply_links.get_meshtastic_for_telegram(
                event.chat_id,
                event.message_id,
            )

        if target_packet_id is None:
            if self._should_emit_missing_target_fallback():
                chunking = context.settings.chunking
                return [
                    SendMeshtasticAction(
                        text=self._reaction_missing_notice(),
                        channel_index=bridge_channel,
                        want_ack=want_ack,
                        retry_max_attempts=chunking.retry_max_attempts,
                        retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                        retry_backoff_factor=chunking.retry_backoff_factor,
                        require_packet_id=True,
                    )
                ]
            return []

        chunking = context.settings.chunking
        return [
            SendMeshtasticReactionAction(
                emoji=event.emoji,
                target_packet_id=target_packet_id,
                channel_index=bridge_channel,
                want_ack=want_ack,
                retry_max_attempts=chunking.retry_max_attempts,
                retry_initial_delay_ms=chunking.retry_initial_delay_ms,
                retry_backoff_factor=chunking.retry_backoff_factor,
            )
        ]

    async def on_meshtastic_reaction(
        self,
        event: MeshtasticReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if not self._reactions_enabled():
            return []

        bridge_channel = self._bridge_channel(context)
        if event.channel_index != bridge_channel:
            return []
        if context.local_node_id and event.from_id == context.local_node_id:
            return []

        telegram_message_id = None
        if context.reply_links is not None:
            telegram_message_id = context.reply_links.get_telegram_for_meshtastic(
                context.telegram_group_id,
                event.target_packet_id,
            )

        if telegram_message_id is None:
            if self._should_emit_missing_target_fallback():
                return [
                    SendTelegramAction(
                        chat_id=context.telegram_group_id,
                        text=self._reaction_missing_notice(),
                    )
                ]
            return []

        return [
            SendTelegramReactionAction(
                chat_id=context.telegram_group_id,
                message_id=telegram_message_id,
                emoji=event.emoji,
            )
        ]

    def _reactions_enabled(self) -> bool:
        value = self.settings.get("reactions_enabled", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _meshtastic_want_ack(self) -> bool:
        value = self.settings.get("meshtastic_want_ack", self.DEFAULT_MESHTASTIC_WANT_ACK)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _missing_target_policy(self) -> str:
        raw = str(self.settings.get("missing_target_policy", "fallback_message")).strip().lower()
        if raw == "fallback_message":
            return raw
        return "fallback_message"

    def _should_emit_missing_target_fallback(self) -> bool:
        return self._missing_target_policy() == "fallback_message"

    def _reply_missing_suffix(self) -> str:
        raw = str(self.settings.get("reply_missing_suffix", self.DEFAULT_REPLY_MISSING_SUFFIX)).strip()
        if raw:
            return raw
        return self.DEFAULT_REPLY_MISSING_SUFFIX

    def _reaction_missing_notice(self) -> str:
        raw = str(
            self.settings.get(
                "reaction_missing_notice_template",
                self.DEFAULT_REACTION_MISSING_NOTICE,
            )
        ).strip()
        if raw:
            return raw
        return self.DEFAULT_REACTION_MISSING_NOTICE


def _compact_display_name(sender_display_name: str) -> str:
    normalized = " ".join(sender_display_name.split())
    if not normalized:
        return sender_display_name
    return normalized.split(" ", 1)[0]


def _chunk_sequence_id(event: TelegramMessageEvent) -> str:
    random_suffix = uuid.uuid4().hex[:8]
    return f"tg-{event.chat_id}-{event.message_id}-{random_suffix}"

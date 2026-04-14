from __future__ import annotations

import logging

from meshgram.plugin import BasePlugin
from meshgram.text_utils import split_for_meshtastic
from meshgram.types import (
    MeshtasticTextEvent,
    PluginAction,
    PluginContext,
    SendMeshtasticAction,
    SendTelegramAction,
    TelegramMessageEvent,
)

LOGGER = logging.getLogger(__name__)


class BridgePlugin(BasePlugin):
    name = "bridge"

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

        template = context.settings.telegram.sender_prefix_template
        try:
            meshtastic_text = template.format(display_name=event.sender_display_name, message=text)
        except (KeyError, ValueError):
            LOGGER.warning(
                "Invalid sender_prefix_template placeholders; expected display_name/message. Falling back."
            )
            meshtastic_text = f"[TG:{event.sender_display_name}] {text}"

        chunking = context.settings.chunking
        chunks = split_for_meshtastic(
            text=meshtastic_text,
            payload_limit=context.meshtastic_payload_limit,
            prefix_template=chunking.prefix_template,
            chunking_enabled=chunking.enabled,
        )

        actions: list[PluginAction] = []
        bridge_channel = self._bridge_channel(context)
        for index, chunk in enumerate(chunks):
            delay_ms = chunking.inter_chunk_delay_ms if index > 0 else 0
            actions.append(
                SendMeshtasticAction(
                    text=chunk,
                    channel_index=bridge_channel,
                    reply_id=meshtastic_reply_id if index == 0 else None,
                    delay_ms=delay_ms,
                    bridge_source_telegram_chat_id=event.chat_id,
                    bridge_source_telegram_message_id=event.message_id,
                    bridge_canonical_for_telegram_message=index == 0,
                )
            )

        return actions

from __future__ import annotations

from meshgram.plugin import BasePlugin
from meshgram.text_utils import normalized_exact_word
from meshgram.types import MeshtasticTextEvent, PluginAction, PluginContext, SendMeshtasticAction


class PingPongPlugin(BasePlugin):
    name = "ping_pong"

    def _allowed_channels(self) -> set[int] | None:
        configured = self.settings.get("channels")
        if configured is None:
            return None

        parsed_values: list[int] = []
        if isinstance(configured, (list, tuple, set)):
            raw_values = configured
        elif isinstance(configured, str):
            raw_values = [part.strip() for part in configured.split(",") if part.strip()]
        else:
            raw_values = [configured]

        for value in raw_values:
            try:
                parsed_values.append(int(value))
            except (TypeError, ValueError):
                continue

        if not parsed_values:
            return set()
        return set(parsed_values)

    def _keyword_responses(self) -> dict[str, str]:
        configured = self.settings.get("keyword_responses")
        if not isinstance(configured, dict):
            response_text = str(self.settings.get("response_text", "Pong"))
            return {"ping": response_text}

        parsed: dict[str, str] = {}
        for raw_keyword, raw_response in configured.items():
            keyword = normalized_exact_word(str(raw_keyword))
            if not keyword:
                continue

            response_text = str(raw_response).strip()
            if not response_text:
                continue

            parsed[keyword] = response_text

        if parsed:
            return parsed

        response_text = str(self.settings.get("response_text", "Pong"))
        return {"ping": response_text}

    async def on_meshtastic_message(
        self,
        event: MeshtasticTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        allowed_channels = self._allowed_channels()
        if allowed_channels is not None and event.channel_index not in allowed_channels:
            return []

        keyword = normalized_exact_word(event.text)
        response_text = self._keyword_responses().get(keyword)
        if response_text is None:
            return []

        return [
            SendMeshtasticAction(
                text=response_text,
                channel_index=event.channel_index,
                reply_id=event.packet_id,
            )
        ]

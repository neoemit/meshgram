from __future__ import annotations

import time

from meshgram.plugin import BasePlugin
from meshgram.text_utils import normalized_exact_word
from meshgram.types import MeshtasticTextEvent, PluginAction, PluginContext, SendMeshtasticAction


class PingPongPlugin(BasePlugin):
    name = "ping_pong"
    DEFAULT_RESPONSE_DEDUPE_TTL_SECONDS = 6.0

    def __init__(self, settings: dict | None = None):
        super().__init__(settings)
        self._recent_keyword_responses: dict[tuple[str, int, str], float] = {}

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

    def _response_dedupe_ttl_seconds(self) -> float:
        raw = self.settings.get(
            "response_dedupe_ttl_seconds",
            self.DEFAULT_RESPONSE_DEDUPE_TTL_SECONDS,
        )
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return self.DEFAULT_RESPONSE_DEDUPE_TTL_SECONDS

    def _sender_dedupe_key(self, event: MeshtasticTextEvent, keyword: str) -> tuple[str, int, str]:
        sender_key = event.from_id or event.sender_label or "unknown"
        return (sender_key, event.channel_index, keyword)

    def _is_duplicate_recent_keyword(self, event: MeshtasticTextEvent, keyword: str) -> bool:
        ttl_seconds = self._response_dedupe_ttl_seconds()
        if ttl_seconds <= 0:
            return False

        now = time.monotonic()
        expiry_cutoff = now - ttl_seconds
        stale_keys = [
            seen_key
            for seen_key, seen_time in self._recent_keyword_responses.items()
            if seen_time < expiry_cutoff
        ]
        for stale_key in stale_keys:
            self._recent_keyword_responses.pop(stale_key, None)

        dedupe_key = self._sender_dedupe_key(event, keyword)
        if dedupe_key in self._recent_keyword_responses:
            return True

        self._recent_keyword_responses[dedupe_key] = now
        return False

    async def on_meshtastic_message(
        self,
        event: MeshtasticTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if event.packet_id is None:
            return []

        allowed_channels = self._allowed_channels()
        if allowed_channels is not None and event.channel_index not in allowed_channels:
            return []

        keyword = normalized_exact_word(event.text)
        response_text = self._keyword_responses().get(keyword)
        if response_text is None:
            return []

        if self._is_duplicate_recent_keyword(event, keyword):
            return []

        return [
            SendMeshtasticAction(
                text=response_text,
                channel_index=event.channel_index,
                reply_id=event.packet_id,
            )
        ]

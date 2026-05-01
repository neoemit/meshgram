from __future__ import annotations

import logging
import time

from meshgram.plugin import BasePlugin
from meshgram.text_utils import normalized_exact_word
from meshgram.types import MeshtasticTextEvent, PluginAction, PluginContext, SendMeshtasticAction

LOGGER = logging.getLogger(__name__)


class PingPongPlugin(BasePlugin):
    name = "ping_pong"
    DEFAULT_RESPONSE_DEDUPE_TTL_SECONDS = 30.0

    def __init__(self, settings: dict | None = None):
        super().__init__(settings)
        self._recent_keyword_responses: dict[tuple[str, str], float] = {}

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

    def _sender_identity(self, event: MeshtasticTextEvent) -> str:
        raw_packet = event.raw_packet if isinstance(event.raw_packet, dict) else {}

        from_num = raw_packet.get("from")
        if isinstance(from_num, int):
            return f"node_num:{from_num & 0xFFFFFFFF:08x}"

        from_id = event.from_id
        if isinstance(from_id, str):
            normalized = from_id.strip().lower()
            if normalized:
                return f"from_id:{normalized}"

        sender_label = event.sender_label
        if isinstance(sender_label, str):
            normalized_label = sender_label.strip().lower()
            if normalized_label:
                return f"sender_label:{normalized_label}"

        return "unknown"

    def _normalized_node_id(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None

        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized.startswith("!"):
            normalized = normalized[1:]
        if normalized.startswith("0x"):
            normalized = normalized[2:]
        if not normalized:
            return None
        return f"!{normalized}"

    def _sender_node_id(self, event: MeshtasticTextEvent) -> str | None:
        raw_packet = event.raw_packet if isinstance(event.raw_packet, dict) else {}
        from_num = raw_packet.get("from")
        if isinstance(from_num, int):
            return f"!{from_num & 0xFFFFFFFF:08x}"

        return self._normalized_node_id(event.from_id)

    def _is_from_local_node(self, event: MeshtasticTextEvent, context: PluginContext) -> bool:
        local_node_id = self._normalized_node_id(getattr(context, "local_node_id", None))
        if local_node_id is None:
            return False

        sender_node_id = self._sender_node_id(event)
        return sender_node_id == local_node_id

    def _sender_dedupe_key(self, event: MeshtasticTextEvent, keyword: str) -> tuple[str, str]:
        sender_key = self._sender_identity(event)
        return (sender_key, keyword)

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
            LOGGER.info(
                "PingPong dedupe suppressed keyword response: keyword=%s packet_id=%s from_id=%s sender_label=%s channel=%s dedupe_key=%s",
                keyword,
                event.packet_id,
                event.from_id,
                event.sender_label,
                event.channel_index,
                dedupe_key,
            )
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

        if self._is_from_local_node(event, context):
            LOGGER.info(
                "PingPong ignored local-node message: keyword=%s packet_id=%s from_id=%s sender_label=%s channel=%s local_node_id=%s",
                keyword,
                event.packet_id,
                event.from_id,
                event.sender_label,
                event.channel_index,
                getattr(context, "local_node_id", None),
            )
            return []

        if self._is_duplicate_recent_keyword(event, keyword):
            return []

        LOGGER.info(
            "PingPong sending keyword response: keyword=%s packet_id=%s from_id=%s sender_label=%s channel=%s",
            keyword,
            event.packet_id,
            event.from_id,
            event.sender_label,
            event.channel_index,
        )

        return [
            SendMeshtasticAction(
                text=response_text,
                channel_index=event.channel_index,
                reply_id=event.packet_id,
            )
        ]

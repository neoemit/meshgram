from __future__ import annotations

import logging
from typing import Any

from meshgram.config import MESHCORE_BACKEND
from meshgram.plugin import BasePlugin
from meshgram.text_utils import normalized_exact_word
from meshgram.types import MeshTextEvent, PluginAction, PluginContext, SendMeshAction

LOGGER = logging.getLogger(__name__)

DIRECT_PATH_SENTINEL = 255
DEFAULT_KEYWORDS = {"trace"}
HEX_CHARS = set("0123456789abcdef")


class TraceMePlugin(BasePlugin):
    name = "trace_me"

    def _keywords(self) -> set[str]:
        configured = self.settings.get("keywords", ["trace"])
        if isinstance(configured, str):
            raw_values = [part.strip() for part in configured.split(",")]
        elif isinstance(configured, (list, tuple, set)):
            raw_values = configured
        else:
            raw_values = [configured]

        parsed: set[str] = set()
        for value in raw_values:
            keyword = normalized_exact_word(str(value))
            if keyword:
                parsed.add(keyword)
        return parsed or DEFAULT_KEYWORDS

    def _allowed_channels(self) -> set[int] | None:
        configured = self.settings.get("channels")
        if configured is None:
            return None

        if isinstance(configured, str):
            raw_values = [part.strip() for part in configured.split(",") if part.strip()]
        elif isinstance(configured, (list, tuple, set)):
            raw_values = configured
        else:
            raw_values = [configured]

        parsed: set[int] = set()
        for value in raw_values:
            try:
                parsed.add(int(value))
            except (TypeError, ValueError):
                continue
        return parsed

    def _response_channel(self, event: MeshTextEvent) -> int:
        raw = self.settings.get("response_channel", self.settings.get("channel"))
        if raw is None:
            return event.channel_index
        if isinstance(raw, str) and raw.strip().lower() in {"", "same", "incoming"}:
            return event.channel_index
        try:
            return int(raw)
        except (TypeError, ValueError):
            LOGGER.warning("Invalid trace_me response_channel=%r; using incoming channel", raw)
            return event.channel_index

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        if context.settings.mesh.backend != MESHCORE_BACKEND:
            return []
        if event.channel_index < 0 or event.to_id is not None:
            return []

        allowed_channels = self._allowed_channels()
        if allowed_channels is not None and event.channel_index not in allowed_channels:
            return []

        keyword = normalized_exact_word(event.text)
        if keyword not in self._keywords():
            return []

        response = format_trace_response(event.raw_packet)
        if response is None:
            LOGGER.info(
                "TraceMe could not format trace response: packet_id=%s from_id=%s channel=%s raw_keys=%s",
                event.packet_id,
                event.from_id,
                event.channel_index,
                sorted(event.raw_packet.keys()) if isinstance(event.raw_packet, dict) else type(event.raw_packet).__name__,
            )
            return []

        return [
            SendMeshAction(
                text=response,
                channel_index=self._response_channel(event),
                reply_id=event.packet_id,
            )
        ]


def split_path_hashes(path_hex: object, path_hash_mode: object) -> list[str]:
    if not isinstance(path_hex, str):
        return []

    normalized = "".join(ch for ch in path_hex.strip().lower() if ch in HEX_CHARS)
    if not normalized:
        return []

    try:
        mode = int(path_hash_mode)
    except (TypeError, ValueError):
        mode = 0
    if mode < 0:
        mode = 0

    hash_size_bytes = mode + 1
    chunk_chars = hash_size_bytes * 2
    if chunk_chars <= 0 or len(normalized) % chunk_chars != 0:
        return []

    return [normalized[i : i + chunk_chars] for i in range(0, len(normalized), chunk_chars)]


def _safe_path_len(raw_packet: dict[str, Any]) -> int | None:
    raw = raw_packet.get("path_len")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None

    if value == DIRECT_PATH_SENTINEL:
        return 0
    if value < 0:
        return None
    return value


def _pluralize_hops(count: int) -> str:
    return "hop" if count == 1 else "hops"


def format_trace_response(raw_packet: dict[str, Any]) -> str | None:
    if not isinstance(raw_packet, dict):
        return None

    path_hashes = split_path_hashes(raw_packet.get("path"), raw_packet.get("path_hash_mode", 0))
    path_len = _safe_path_len(raw_packet)

    if path_hashes:
        count = len(path_hashes)
        if path_len not in (None, count):
            LOGGER.warning("TraceMe path_len/path mismatch: path_len=%s hashes=%s", path_len, path_hashes)
            count = path_len
        return f"{','.join(path_hashes)} ({count} {_pluralize_hops(count)})"

    if path_len is None:
        return None
    if path_len == 0:
        return "0 hops"
    return f"{path_len} {_pluralize_hops(path_len)} (repeater list unavailable)"

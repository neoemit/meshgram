from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class _TelegramToMeshtasticLink:
    expires_at: float
    meshtastic_packet_id: int


@dataclass(slots=True)
class _MeshtasticToTelegramLink:
    expires_at: float
    chat_id: int
    telegram_message_id: int


class ReplyLinkRegistry:
    """In-memory bidirectional reply mapping with TTL pruning."""

    def __init__(self, ttl_hours: int = 24):
        if ttl_hours <= 0:
            ttl_hours = 24

        self.ttl_seconds = int(ttl_hours * 60 * 60)
        self._telegram_to_meshtastic: dict[tuple[int, int], _TelegramToMeshtasticLink] = {}
        self._meshtastic_to_telegram: dict[int, _MeshtasticToTelegramLink] = {}

    def _now(self) -> float:
        return time.time()

    def _prune_expired(self, now: Optional[float] = None) -> None:
        now_ts = self._now() if now is None else now

        expired_tg_keys = [
            key
            for key, value in self._telegram_to_meshtastic.items()
            if value.expires_at <= now_ts
        ]
        for key in expired_tg_keys:
            self._telegram_to_meshtastic.pop(key, None)

        expired_mesh_keys = [
            key
            for key, value in self._meshtastic_to_telegram.items()
            if value.expires_at <= now_ts
        ]
        for key in expired_mesh_keys:
            self._meshtastic_to_telegram.pop(key, None)

    def link_telegram_to_meshtastic(
        self,
        chat_id: int,
        telegram_message_id: int,
        meshtastic_packet_id: int,
    ) -> None:
        now_ts = self._now()
        self._prune_expired(now_ts)

        self._telegram_to_meshtastic[(chat_id, telegram_message_id)] = _TelegramToMeshtasticLink(
            expires_at=now_ts + self.ttl_seconds,
            meshtastic_packet_id=meshtastic_packet_id,
        )

    def link_meshtastic_to_telegram(
        self,
        meshtastic_packet_id: int,
        chat_id: int,
        telegram_message_id: int,
    ) -> None:
        now_ts = self._now()
        self._prune_expired(now_ts)

        self._meshtastic_to_telegram[meshtastic_packet_id] = _MeshtasticToTelegramLink(
            expires_at=now_ts + self.ttl_seconds,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
        )

    def get_meshtastic_for_telegram(
        self,
        chat_id: int,
        telegram_message_id: int,
    ) -> Optional[int]:
        self._prune_expired()

        link = self._telegram_to_meshtastic.get((chat_id, telegram_message_id))
        if link is None:
            return None

        return link.meshtastic_packet_id

    def get_telegram_for_meshtastic(
        self,
        chat_id: int,
        meshtastic_packet_id: int,
    ) -> Optional[int]:
        self._prune_expired()

        link = self._meshtastic_to_telegram.get(meshtastic_packet_id)
        if link is None:
            return None
        if link.chat_id != chat_id:
            return None

        return link.telegram_message_id

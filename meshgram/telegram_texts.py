"""The texts of recent Telegram messages, kept across restarts.

A reaction update only names the message reacted to (chat + message id); the Bot API
has no way to fetch a message, so the bridge quotes it from here. Saved to the data
directory so reactions to messages from before a restart can still be relayed.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .storage import write_private_json

LOGGER = logging.getLogger(__name__)

TELEGRAM_TEXTS_FILE = "telegram_messages.json"
FILE_VERSION = 1
# How many messages are remembered.
TELEGRAM_TEXT_CACHE_SIZE = 500


class TelegramTextCache:
    """The last ``size`` message texts by (chat id, message id), oldest dropped first.

    Thread-safe: ``save`` may run in a worker thread while the event loop adds texts.
    """

    def __init__(self, path: Optional[Path], size: int):
        self.path = path
        self.size = max(1, size)
        self._texts: OrderedDict[tuple[int, int], str] = OrderedDict()
        self._lock = threading.Lock()
        self._dirty = False
        self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._texts)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def get(self, chat_id: int, message_id: int) -> Optional[str]:
        with self._lock:
            return self._texts.get((chat_id, message_id))

    def remember(self, chat_id: int, message_id: int, text: str) -> None:
        key = (chat_id, message_id)
        with self._lock:
            self._texts[key] = text
            self._texts.move_to_end(key)
            while len(self._texts) > self.size:
                self._texts.popitem(last=False)
            self._dirty = True

    def save(self) -> None:
        """Write the texts if they changed since the last save (blocking: file I/O)."""
        if self.path is None:
            return
        # Held while writing, so the last save to finish always has the latest texts.
        with self._lock:
            if not self._dirty:
                return
            data = {
                "version": FILE_VERSION,
                "messages": [[chat_id, message_id, text] for (chat_id, message_id), text in self._texts.items()],
            }
            self._dirty = False
            try:
                write_private_json(self.path, data)
            except OSError as exc:
                self._dirty = True
                LOGGER.warning("Couldn't save recent Telegram messages to %s: %s", self.path, exc)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            messages = data["messages"] if isinstance(data, dict) else None
            if not isinstance(messages, list):
                raise ValueError("no message list")
        except (OSError, ValueError, KeyError) as exc:
            LOGGER.warning("Ignoring unreadable recent Telegram messages in %s: %s", self.path, exc)
            return
        for entry in messages[-self.size:]:
            if (
                isinstance(entry, list)
                and len(entry) == 3
                and all(isinstance(value, int) and not isinstance(value, bool) for value in entry[:2])
                and isinstance(entry[2], str)
            ):
                self._texts[(entry[0], entry[1])] = entry[2]

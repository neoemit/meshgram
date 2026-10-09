"""Connection status of everything Meshgram talks to (radio, Telegram, MQTT, ...).

The app and plugins report state changes here; the packet_map web app shows them.
``set_state`` may be called from any thread (paho's network thread, for example):
listeners always run on the event loop they were registered from.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)

CONNECTED = "connected"
CONNECTING = "connecting"
DISCONNECTED = "disconnected"
# Configured off, or unavailable (e.g. the broker refused the subscription).
DISABLED = "disabled"
STATES = {CONNECTED, CONNECTING, DISCONNECTED, DISABLED}

# Display order; services not listed here come after, in registration order.
SERVICE_ORDER = ("radio", "telegram", "mqtt_publish", "mqtt_subscribe", "meshmapper_feed")

StatusListener = Callable[[dict[str, Any]], None]


class StatusRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._services: dict[str, dict[str, Any]] = {}
        self._listeners: list[tuple[StatusListener, asyncio.AbstractEventLoop]] = []

    def set_state(self, key: str, state: str, detail: str = "", label: Optional[str] = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown connection state: {state}")
        with self._lock:
            current = self._services.get(key)
            label = label or (current or {}).get("label") or key
            if current is not None and (current["state"], current["detail"], current["label"]) == (state, detail, label):
                return
            since = current["since"] if current is not None and current["state"] == state else time.time()
            entry = {"key": key, "label": label, "state": state, "detail": detail, "since": since}
            self._services[key] = entry
            listeners = list(self._listeners)
        for listener, loop in listeners:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(listener, dict(entry))
            except RuntimeError:
                # The loop shut down between the check and the call.
                pass

    def get(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self._services.get(key)
            return dict(entry) if entry is not None else None

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = [dict(entry) for entry in self._services.values()]
        rank = {key: index for index, key in enumerate(SERVICE_ORDER)}
        return sorted(entries, key=lambda entry: rank.get(entry["key"], len(rank)))

    def add_listener(self, listener: StatusListener) -> None:
        """Register ``listener(entry)``; must be called from the event loop it should run on."""
        loop = asyncio.get_running_loop()
        with self._lock:
            self._listeners.append((listener, loop))

    def remove_listener(self, listener: StatusListener) -> None:
        with self._lock:
            self._listeners = [(cb, loop) for cb, loop in self._listeners if cb != listener]

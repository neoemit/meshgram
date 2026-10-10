from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .config import canonical_plugin_name
from .types import (
    MeshTextEvent,
    PluginAction,
    PluginContext,
    TelegramMessageEvent,
)

if TYPE_CHECKING:
    from .transport import MeshCoreTransport

BUILTIN_PLUGINS: dict[str, str] = {
    "bridge": "meshgram.plugins.bridge:BridgePlugin",
    "ping_pong": "meshgram.plugins.ping_pong:PingPongPlugin",
    "dm_http_command": "meshgram.plugins.dm_http_command:DirectMessageHttpCommandPlugin",
    "trace_me": "meshgram.plugins.trace_me:TraceMePlugin",
    "meshmapper": "meshgram.plugins.meshmapper:MeshMapperPlugin",
    "packet_map": "meshgram.plugins.packet_map:PacketMapPlugin",
}

# Schema keyword the control panel understands: an integer that is a radio
# channel slot, picked by name from the radio's channels.
CHANNEL_FORMAT = "channel"


class BasePlugin:
    name = "base"
    # Shown in the web app's control panel.
    title = ""
    description = ""
    # JSON Schema of ``settings`` (see meshgram.settings_schema). The control
    # panel renders a form from it and validates edits against it.
    settings_schema: dict[str, Any] = {"type": "object"}

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        return []

    async def on_mesh_connected(
        self,
        transport: "MeshCoreTransport",
        context: PluginContext,
    ) -> None:
        """Called after every successful (re)connect to the radio, and on start if it's already connected."""

    async def on_shutdown(self) -> None:
        """Called when the plugin is turned off, reconfigured (it's restarted) or the app stops.

        Release background resources and unregister transport listeners here.
        """

    async def apply_settings(self, settings: dict[str, Any]) -> bool:
        """Take new settings (from the web app) while running, if the plugin can.

        Return True once applied (also update ``self.settings``); False, the
        default, has the plugin restarted with them instead.
        """
        return False

    async def on_telegram_message(
        self,
        event: TelegramMessageEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        return []

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        return []


def plugin_key(name: str) -> str:
    """The name a plugin is known by: built-ins by their canonical name, others as configured."""
    canonical = canonical_plugin_name(name)
    return canonical if canonical in BUILTIN_PLUGINS else str(name).strip()


def resolve_plugin_target(name: str) -> str:
    """``module:Class`` for a plugin name (a built-in name, ``module:Class`` or a module with a ``Plugin`` class)."""
    target = BUILTIN_PLUGINS.get(plugin_key(name), name)
    if ":" in target:
        return target
    return f"{target}:Plugin"


def load_plugin_class(name: str) -> type:
    module_name, class_name = resolve_plugin_target(name).split(":", maxsplit=1)
    return getattr(importlib.import_module(module_name), class_name)

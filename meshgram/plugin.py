from __future__ import annotations

import importlib
import re
from typing import TYPE_CHECKING, Any

from .config import canonical_plugin_name
from .types import (
    MeshTextEvent,
    PluginAction,
    PluginContext,
    TelegramButtonEvent,
    TelegramMessageEvent,
    TelegramReactionEvent,
)

if TYPE_CHECKING:
    from .transport import MeshCoreTransport

BUILTIN_PLUGINS: dict[str, str] = {
    "bridge": "meshgram.plugins.bridge:BridgePlugin",
    "ping_pong": "meshgram.plugins.ping_pong:PingPongPlugin",
    "dm_http_command": "meshgram.plugins.dm_http_command:DirectMessageHttpCommandPlugin",
    "trace_me": "meshgram.plugins.trace_me:TraceMePlugin",
    "meshmapper": "meshgram.plugins.meshmapper:MeshMapperPlugin",
    "letsmesh": "meshgram.plugins.letsmesh:LetsMeshPlugin",
    "meshrank": "meshgram.plugins.meshrank:MeshRankPlugin",
    "custom_mqtt": "meshgram.plugins.custom_mqtt:CustomMqttPlugin",
    "packet_map": "meshgram.plugins.packet_map:PacketMapPlugin",
    "check_in": "meshgram.plugins.check_in:CheckInPlugin",
}

# "bridge@sos" is a second instance of the bridge plugin, called "sos" (as systemd names
# template units). Only plugins that say ``multi_instance`` can have them.
INSTANCE_SEPARATOR = "@"
INSTANCE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_]{0,23}")

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
    # True: config.yaml and the control panel may run more instances of it ("<name>@<id>"),
    # each with its own settings. The plugin must then keep whatever it names (status
    # entries, routes, files) apart per instance: see ``instance_id``.
    multi_instance = False
    # Set by the plugin manager before ``on_startup``: None for the plugin itself.
    instance_id: str | None = None

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}

    @property
    def instance_name(self) -> str:
        """The name this instance runs under: ``name``, or ``name@instance_id``."""
        return instance_key(self.name, self.instance_id)

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

    async def on_telegram_reaction(
        self,
        event: TelegramReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        return []

    async def on_telegram_button(
        self,
        event: TelegramButtonEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        """A button under one of the bot's messages was pressed. Every plugin hears every press:
        act on your own buttons only (by their callback data), and answer the press."""
        return []

    async def on_mesh_message(
        self,
        event: MeshTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        return []


def instance_key(base: str, instance_id: str | None) -> str:
    return f"{base}{INSTANCE_SEPARATOR}{instance_id}" if instance_id else base


def split_instance(name: str) -> tuple[str, str | None]:
    """``("bridge", "sos")`` for "bridge@sos"; ``(name, None)`` for a plugin itself.

    Only built-in plugins have instances: any other name (a module path) is taken whole.
    """
    canonical = canonical_plugin_name(name)
    base, separator, instance_id = canonical.partition(INSTANCE_SEPARATOR)
    if separator and base in BUILTIN_PLUGINS:
        return base, instance_id.lower()
    return (canonical if canonical in BUILTIN_PLUGINS else str(name).strip()), None


def plugin_key(name: str) -> str:
    """The name a plugin is known by: built-ins by their canonical name (with "@<instance>"
    for a further instance), others as configured."""
    return instance_key(*split_instance(name))


def resolve_plugin_target(name: str) -> str:
    """``module:Class`` for a plugin name (a built-in name, ``module:Class`` or a module with a ``Plugin`` class)."""
    base, _ = split_instance(name)
    target = BUILTIN_PLUGINS.get(base, name)
    if ":" in target:
        return target
    return f"{target}:Plugin"


def load_plugin_class(name: str) -> type:
    module_name, class_name = resolve_plugin_target(name).split(":", maxsplit=1)
    return getattr(importlib.import_module(module_name), class_name)

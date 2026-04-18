from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

from .config import PluginConfig
from .types import (
    MeshtasticReactionEvent,
    MeshtasticTextEvent,
    Plugin,
    PluginAction,
    PluginContext,
    TelegramMessageEvent,
    TelegramReactionEvent,
)

LOGGER = logging.getLogger(__name__)

BUILTIN_PLUGINS: dict[str, str] = {
    "bridge": "meshgram.plugins.bridge:BridgePlugin",
    "ping_pong": "meshgram.plugins.ping_pong:PingPongPlugin",
    "dm_http_command": "meshgram.plugins.dm_http_command:DirectMessageHttpCommandPlugin",
}


class BasePlugin:
    name = "base"

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        return []

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

    async def on_meshtastic_message(
        self,
        event: MeshtasticTextEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        return []

    async def on_meshtastic_reaction(
        self,
        event: MeshtasticReactionEvent,
        context: PluginContext,
    ) -> list[PluginAction]:
        return []


@dataclass(slots=True)
class LoadedPlugin:
    name: str
    instance: Plugin


def _resolve_plugin_target(name: str) -> str:
    target = BUILTIN_PLUGINS.get(name, name)
    if ":" in target:
        return target

    return f"{target}:Plugin"


def load_plugins(plugin_configs: list[PluginConfig]) -> list[LoadedPlugin]:
    plugins: list[LoadedPlugin] = []

    for plugin_config in plugin_configs:
        if not plugin_config.enabled:
            continue

        target = _resolve_plugin_target(plugin_config.name)
        module_name, class_name = target.split(":", maxsplit=1)

        module = importlib.import_module(module_name)
        plugin_class = getattr(module, class_name)
        instance = plugin_class(plugin_config.settings)

        plugins.append(LoadedPlugin(name=plugin_config.name, instance=instance))
        LOGGER.info("Loaded plugin %s (%s)", plugin_config.name, target)

    return plugins

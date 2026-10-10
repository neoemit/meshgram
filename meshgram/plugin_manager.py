"""Runs the plugins, and turns them on and off or reconfigures them at runtime.

Plugin settings come from config.yaml. Changes made in the web app are saved as
overrides in ``$MESHGRAM_DATA_DIR/plugins.json`` and win over config.yaml until
they're reset there. config.yaml itself is never rewritten: it holds secrets
and is usually mounted read-only. The web app offers config.yaml with the
overrides in it instead (``config_export``); once config.yaml says the same as
an override, the override is dropped on startup.

Reconfiguring a plugin restarts it: the running instance gets ``on_shutdown``,
a new one is created with the new settings and gets ``on_startup`` (and
``on_mesh_connected`` when the radio is connected). A plugin that can take the
new settings while running says so from ``apply_settings``, and keeps running.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol

from .config import PluginConfig
from .plugin import BUILTIN_PLUGINS, load_plugin_class, plugin_key
from .settings_schema import SettingsError, mask_secrets, restore_secrets, validate
from .types import Plugin, PluginAction, PluginContext

if TYPE_CHECKING:
    from .transport import MeshCoreTransport

LOGGER = logging.getLogger(__name__)

OVERRIDES_FILE = "plugins.json"
OVERRIDES_VERSION = 1


class UnknownPluginError(KeyError):
    pass


class PluginHost(Protocol):
    def plugin_context(self) -> PluginContext:
        ...

    async def execute_actions(self, actions: list[PluginAction], plugin_name: str) -> None:
        ...

    def connected_transport(self) -> Optional["MeshCoreTransport"]:
        ...


class PluginOverrideStore:
    """Plugin settings changed in the web app, as JSON next to the other persistent state."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            LOGGER.error("Ignoring plugin settings saved from the web app: can't read %s (%s)", self.path, exc)
            return {}
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, dict):
            return {}
        return {str(name): value for name, value in plugins.items() if isinstance(value, dict)}

    def save(self, overrides: dict[str, dict[str, Any]]) -> None:
        """Replace the file atomically; it may hold secrets, so only its owner can read it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": OVERRIDES_VERSION, "plugins": overrides}, indent=2, sort_keys=True)
        descriptor, temp_path = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise


@dataclass(slots=True)
class PluginEntry:
    name: str
    in_config: bool
    config_enabled: bool
    config_settings: dict[str, Any]
    # What the web app changed: "enabled" and/or "settings".
    override: dict[str, Any] = field(default_factory=dict)
    instance: Optional[Plugin] = None
    error: Optional[str] = None
    plugin_class: Optional[type] = None
    load_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.override.get("enabled", self.config_enabled))

    @property
    def settings(self) -> dict[str, Any]:
        settings = self.override.get("settings", self.config_settings)
        return settings if isinstance(settings, dict) else {}

    @property
    def schema(self) -> dict[str, Any]:
        schema = getattr(self.plugin_class, "settings_schema", None)
        return schema if isinstance(schema, dict) else {"type": "object"}


def _differences(entry: PluginEntry, override: dict[str, Any]) -> dict[str, Any]:
    """The part of ``override`` that differs from config.yaml."""
    override = dict(override)
    if override.get("enabled", entry.config_enabled) == entry.config_enabled:
        override.pop("enabled", None)
    if "settings" in override and override["settings"] == entry.config_settings:
        override.pop("settings")
    return override


class PluginManager:
    def __init__(
        self,
        configs: list[PluginConfig],
        host: PluginHost,
        store: Optional[PluginOverrideStore] = None,
    ):
        self.host = host
        self.store = store
        self._entries: dict[str, PluginEntry] = {}
        self._lock = asyncio.Lock()

        for config in configs:
            name = plugin_key(config.name)
            if name in self._entries:
                LOGGER.warning("Plugin %s is configured more than once; using the first entry", name)
                continue
            self._entries[name] = PluginEntry(
                name=name,
                in_config=True,
                config_enabled=config.enabled,
                config_settings=dict(config.settings),
            )
        # Built-ins config.yaml doesn't mention can still be turned on from the web app.
        for name in BUILTIN_PLUGINS:
            self._entries.setdefault(
                name, PluginEntry(name=name, in_config=False, config_enabled=False, config_settings={})
            )

        overrides = store.load() if store is not None else {}
        caught_up: list[str] = []
        for name, override in overrides.items():
            entry = self._entries.get(plugin_key(name))
            if entry is None:
                LOGGER.warning("Ignoring saved settings for unknown plugin %s", name)
                continue
            saved = {key: override[key] for key in ("enabled", "settings") if key in override}
            entry.override = _differences(entry, saved)
            if entry.override != saved:
                caught_up.append(entry.name)
            if entry.override:
                LOGGER.info(
                    "Plugin %s: using %s saved from the web app (reset it there to use config.yaml)",
                    entry.name,
                    " and ".join("its settings" if key == "settings" else "on/off state" for key in entry.override),
                )

        if caught_up and store is not None:
            # config.yaml has the web app's changes now (copied from its export, say).
            LOGGER.info("Plugin settings saved from the web app that config.yaml now has: dropped (%s)", ", ".join(caught_up))
            try:
                store.save(self.overrides())
            except OSError as exc:
                LOGGER.warning("Couldn't update %s: %s", store.path, exc)

        for entry in self._entries.values():
            self._load_class(entry)

    # --- Queries --------------------------------------------------------------

    @property
    def running(self) -> list[tuple[str, Plugin]]:
        """Running plugins in dispatch order (a copy, so it's safe to iterate while plugins change)."""
        return [(entry.name, entry.instance) for entry in self._entries.values() if entry.instance is not None]

    def get(self, name: str) -> Optional[Plugin]:
        entry = self._entries.get(plugin_key(name))
        return entry.instance if entry is not None else None

    def describe(self, name: str) -> dict[str, Any]:
        return self._describe(self._entry(name))

    def catalog(self) -> list[dict[str, Any]]:
        return [self._describe(entry) for entry in self._entries.values()]

    def overrides(self) -> dict[str, dict[str, Any]]:
        """The web app's changes by plugin (``enabled`` and/or ``settings``), secrets included."""
        return {entry.name: copy.deepcopy(entry.override) for entry in self._entries.values() if entry.override}

    def _entry(self, name: str) -> PluginEntry:
        entry = self._entries.get(plugin_key(name))
        if entry is None:
            raise UnknownPluginError(name)
        return entry

    def _describe(self, entry: PluginEntry) -> dict[str, Any]:
        plugin_class = entry.plugin_class
        return {
            "name": entry.name,
            "title": getattr(plugin_class, "title", "") or entry.name,
            "description": getattr(plugin_class, "description", ""),
            "builtin": entry.name in BUILTIN_PLUGINS,
            "in_config": entry.in_config,
            "enabled": entry.enabled,
            "running": entry.instance is not None,
            "error": entry.load_error or entry.error,
            "overridden": sorted(entry.override),
            "settings": mask_secrets(entry.settings, entry.schema),
            "schema": entry.schema,
        }

    # --- Lifecycle --------------------------------------------------------------

    async def start_all(self) -> None:
        async with self._lock:
            for entry in self._entries.values():
                if entry.enabled:
                    await self._start(entry)

    async def stop_all(self) -> None:
        async with self._lock:
            for entry in reversed(list(self._entries.values())):
                await self._stop(entry)

    async def mesh_connected(self, transport: "MeshCoreTransport", context: PluginContext) -> None:
        for name, instance in self.running:
            await self._call_mesh_connected(name, instance, transport, context)

    def _load_class(self, entry: PluginEntry) -> None:
        try:
            entry.plugin_class = load_plugin_class(entry.name)
            entry.load_error = None
        except Exception as exc:
            entry.plugin_class = None
            entry.load_error = f"Can't load the plugin: {exc}"
            if entry.enabled:
                LOGGER.exception("Plugin %s can't be loaded", entry.name)

    async def _start(self, entry: PluginEntry) -> None:
        if entry.plugin_class is None:
            return  # It couldn't be loaded; load_error says why.
        try:
            # A copy: the plugin may keep and change it, the entry must not change behind its back.
            instance = entry.plugin_class(copy.deepcopy(entry.settings))
        except Exception as exc:
            LOGGER.exception("Plugin %s failed to start", entry.name)
            entry.error = f"Failed to start: {exc}"
            return
        context = self.host.plugin_context()
        try:
            actions = await instance.on_startup(context)
        except Exception as exc:
            LOGGER.exception("Plugin %s failed to start", entry.name)
            entry.error = f"Failed to start: {exc}"
            # Release whatever it set up before failing.
            with contextlib.suppress(Exception):
                await instance.on_shutdown()
            return

        entry.instance = instance
        entry.error = None
        LOGGER.info("Started plugin %s", entry.name)
        await self.host.execute_actions(actions, entry.name)

        transport = self.host.connected_transport()
        if transport is not None:
            await self._call_mesh_connected(entry.name, instance, transport, context)

    async def _stop(self, entry: PluginEntry) -> None:
        instance, entry.instance = entry.instance, None
        if instance is None:
            return
        hook = getattr(instance, "on_shutdown", None)
        if callable(hook):
            try:
                await hook()
            except Exception:
                LOGGER.exception("Plugin %s failed during shutdown", entry.name)
        LOGGER.info("Stopped plugin %s", entry.name)

    @staticmethod
    async def _call_mesh_connected(
        name: str, instance: Plugin, transport: "MeshCoreTransport", context: PluginContext
    ) -> None:
        hook = getattr(instance, "on_mesh_connected", None)
        if not callable(hook):
            return
        try:
            await hook(transport, context)
        except Exception:
            LOGGER.exception("Plugin %s failed handling mesh connect", name)

    # --- Changes from the web app --------------------------------------------------

    async def update(
        self,
        name: str,
        *,
        enabled: Optional[bool] = None,
        settings: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Turn a plugin on/off and/or replace its settings; saved, then applied right away.

        Raises ``SettingsError`` for settings that don't match the plugin's schema.
        """
        async with self._lock:
            entry = self._entry(name)
            override = dict(entry.override)
            if settings is not None:
                if not isinstance(settings, dict):
                    raise SettingsError([("", "must be an object")])
                settings = restore_secrets(settings, entry.settings, entry.schema)
                validate(settings, entry.schema)
                override["settings"] = settings
            if enabled is not None:
                override["enabled"] = bool(enabled)
            override = _differences(entry, override)

            self._save(entry.name, override)
            previous_settings = entry.settings
            entry.override = override
            restarted = await self._apply(entry, settings_changed=entry.settings != previous_settings)
            return {**self._describe(entry), "restarted": restarted}

    async def reset(self, name: str) -> dict[str, Any]:
        """Drop the web app's changes to a plugin and go back to config.yaml."""
        async with self._lock:
            entry = self._entry(name)
            restarted = False
            if entry.override:
                self._save(entry.name, {})
                previous_settings = entry.settings
                entry.override = {}
                restarted = await self._apply(entry, settings_changed=entry.settings != previous_settings)
            return {**self._describe(entry), "restarted": restarted}

    def _save(self, name: str, override: dict[str, Any]) -> None:
        if self.store is None:
            return
        overrides = {key: entry.override for key, entry in self._entries.items() if entry.override}
        if override:
            overrides[name] = override
        else:
            overrides.pop(name, None)
        # Raises OSError when it can't be saved; nothing has changed then.
        self.store.save(overrides)

    async def _apply(self, entry: PluginEntry, *, settings_changed: bool) -> bool:
        """Bring the running instance in line with the entry; True if it was (re)started."""
        if entry.instance is not None and not entry.enabled:
            await self._stop(entry)
        elif entry.instance is not None and settings_changed:
            if await self._apply_live(entry):
                return False
            await self._stop(entry)
        if not entry.enabled:
            entry.error = None
            return False
        if entry.instance is None:
            await self._start(entry)
            return entry.instance is not None
        return False

    async def _apply_live(self, entry: PluginEntry) -> bool:
        """Hand the new settings to the running instance; False when it has to be restarted."""
        hook = getattr(entry.instance, "apply_settings", None)
        if not callable(hook):
            return False
        try:
            applied = bool(await hook(copy.deepcopy(entry.settings)))
        except Exception:
            LOGGER.exception("Plugin %s failed to apply new settings; restarting it", entry.name)
            return False
        if applied:
            LOGGER.info("Plugin %s took its new settings without a restart", entry.name)
        return applied

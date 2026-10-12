"""Meshgram's own settings (everything in config.yaml but the plugins), changed from the control panel.

config.yaml is never rewritten: it holds secrets and is usually mounted
read-only. Settings changed in the control panel are saved as overrides in
``$MESHGRAM_DATA_DIR/settings.json`` (dotted paths such as
``meshcore.connection.serial_device`` to values) and win over config.yaml
until they're reset there. They take effect when Meshgram restarts, which the
control panel can do. Its Config file tab offers config.yaml with them written
in (``config_export``); once config.yaml says the same as an override, the
override is dropped on startup.

Values are compared the way Meshgram reads them (``core_values``), so
``group_id: "-100"`` in config.yaml and ``-100`` from the control panel are the
same setting.
"""
from __future__ import annotations

import asyncio
import copy
import glob
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from .config import ConfigError, MeshgramSettings, build_settings, with_overrides
from .plugin import CHANNEL_FORMAT
from .settings_schema import SettingsError, mask_secrets, restore_secrets, validate
from .storage import write_private_json

LOGGER = logging.getLogger(__name__)

SETTINGS_FILE = "settings.json"
SETTINGS_VERSION = 1
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SERIAL_PORT_PATTERNS = ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usb*")

# Checks a Telegram bot token and group before they're saved; raises SettingsError
# (paths relative to the telegram section) when Telegram turns them down.
TelegramCheck = Callable[[str, int], Awaitable[None]]


def _string(title: str, description: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "title": title, **({"description": description} if description else {}), **extra}


def _integer(title: str, description: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "title": title, **({"description": description} if description else {}), **extra}


def _number(title: str, description: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "number", "title": title, **({"description": description} if description else {}), **extra}


def _boolean(title: str, description: str = "") -> dict[str, Any]:
    return {"type": "boolean", "title": title, **({"description": description} if description else {})}


# One entry per top-level section of config.yaml; plugins have their own schemas.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "telegram": {
            "type": "object",
            "title": "Telegram",
            "description": "The bot and the group it bridges to.",
            "additionalProperties": False,
            "required": ["bot_token", "group_id"],
            "properties": {
                "bot_token": _string(
                    "Bot token",
                    "From @BotFather. Telegram is asked whether it's valid before it's saved.",
                    writeOnly=True,
                    minLength=1,
                    pattern=r"^\d+:[A-Za-z0-9_-]+$",
                    **{"x-pattern-message": "looks like 123456789:ABC… (from @BotFather)"},
                ),
                "group_id": _integer(
                    "Group ID",
                    "The group the bot bridges to: forward one of its messages to @userinfobot. Supergroups start with -100.",
                ),
                "include_captions": _boolean("Relay captions", "Send the captions of photos and other media to the radio."),
                "sender_prefix_template": _string(
                    "Sender prefix",
                    "How Telegram messages read on the radio; {display_name} and {message} are filled in.",
                    minLength=1,
                ),
            },
        },
        "meshcore": {
            "type": "object",
            "title": "Radio",
            "description": "The MeshCore companion radio and how Meshgram reaches it.",
            "additionalProperties": False,
            "properties": {
                "connection": {
                    "type": "object",
                    "title": "Connection",
                    "additionalProperties": False,
                    "required": ["mode"],
                    "properties": {
                        "mode": {
                            "type": "string",
                            "title": "Connect over",
                            "enum": ["serial", "tcp", "ble"],
                            "x-enum-titles": ["USB serial", "TCP (network)", "Bluetooth LE"],
                        },
                        "serial_device": _string(
                            "Serial device",
                            "Serial only. Such as /dev/ttyACM0 or /dev/ttyUSB0; empty to find it automatically. "
                            "In Docker the device must be passed to the container too.",
                        ),
                        "baudrate": _integer("Baud rate", "Serial only. Usually 115200.", minimum=1),
                        "tcp_host": _string("TCP host", "TCP only."),
                        "tcp_port": _integer("TCP port", "TCP only.", minimum=1, maximum=65535),
                        "ble_address": _string("Bluetooth address", "Bluetooth only. Such as 12:34:56:78:90:AB."),
                        "ble_pin": _string("Bluetooth PIN", "Bluetooth only, when the radio asks for one.", writeOnly=True),
                        "auto_reconnect": _boolean("Reconnect automatically"),
                    },
                },
                "bridge_channel": _integer(
                    "Bridge channel",
                    "The channel bridged to the Telegram group, unless the bridge plugin's settings name another.",
                    minimum=0,
                    format=CHANNEL_FORMAT,
                ),
                "contact_name_overrides": {
                    "type": "object",
                    "title": "Contact names",
                    "description": "Names for senders of direct messages, by public key prefix.",
                    "additionalProperties": {"type": "string", "minLength": 1},
                    "x-key-title": "Key prefix",
                    "x-value-title": "Name",
                },
                "outbound_echo_text_fallback_enabled": _boolean(
                    "Echo fallback",
                    "Spot the radio's echoes of our own channel messages by their text. Leave off unless "
                    "your radio leaves out who sent a message and Telegram messages come back to the group.",
                ),
                "outbound_echo_text_fallback_ttl_seconds": _number(
                    "Echo fallback window (s)", "How long a sent message's text is looked out for.", minimum=0
                ),
            },
        },
        "web": {
            "type": "object",
            "title": "Web app",
            "description": "This web app: the map, messages and control panel.",
            "additionalProperties": False,
            "properties": {
                "enabled": _boolean(
                    "Web app on", "Off, this page is gone after the restart: only config.yaml or settings.json can turn it back on."
                ),
                "host": _string(
                    "Listen on",
                    "127.0.0.1 keeps it on this machine; 0.0.0.0 lets other machines in (needed in Docker). "
                    "Beyond this machine the control panel is read-only without a password.",
                    minLength=1,
                ),
                "port": _integer("Port", minimum=1, maximum=65535),
                "password": _string("Password", "HTTP Basic auth, any user name. Empty: no password.", writeOnly=True),
                "title": _string("Title", minLength=1),
                "tile_url": _string("Map tiles URL", "Empty: OpenStreetMap, restyled for the light or dark theme."),
                "tile_attribution": _string("Map tiles attribution"),
            },
        },
        "chunking": {
            "type": "object",
            "title": "Message chunking",
            "description": "How long Telegram messages are split up to fit radio packets.",
            "additionalProperties": False,
            "properties": {
                "enabled": _boolean("Split long messages"),
                "prefix_template": _string("Part prefix", "{index} and {total} are filled in."),
                "max_chunk_bytes": _integer("Largest part (bytes)", minimum=0),
                "broadcast_max_chunk_bytes": _integer("Largest channel message part (bytes)", minimum=0),
                "payload_safety_margin_bytes": _integer("Safety margin (bytes)", minimum=0),
                "inter_chunk_delay_ms": _integer("Pause between parts (ms)", minimum=0),
                "broadcast_min_inter_chunk_delay_ms": _integer("Pause between channel message parts (ms)", minimum=0),
                "retry_max_attempts": _integer("Send attempts", minimum=1),
                "retry_initial_delay_ms": _integer("First retry after (ms)", minimum=0),
                "retry_backoff_factor": _number("Retry backoff factor", minimum=1),
                "abort_on_chunk_failure": _boolean("Give up on a message when a part fails"),
            },
        },
        "runtime": {
            "type": "object",
            "title": "Logging",
            "additionalProperties": False,
            "properties": {
                "log_level": {"type": "string", "title": "Log level", "enum": LOG_LEVELS, "default": "INFO"},
            },
        },
    },
}

SECTIONS = tuple(SCHEMA["properties"])


class UnknownSectionError(KeyError):
    pass


def core_values(settings: MeshgramSettings) -> dict[str, Any]:
    """The settings ``SCHEMA`` describes, as Meshgram reads them (unset optional strings are "")."""
    connection = settings.meshcore.connection
    chunking = settings.chunking
    web = settings.web
    return {
        "telegram": {
            "bot_token": settings.telegram_bot_token,
            "group_id": settings.telegram_group_id,
            "include_captions": settings.telegram.include_captions,
            "sender_prefix_template": settings.telegram.sender_prefix_template,
        },
        "meshcore": {
            "connection": {
                "mode": connection.mode,
                "serial_device": connection.serial_device or "",
                "baudrate": connection.baudrate,
                "tcp_host": connection.tcp_host,
                "tcp_port": connection.tcp_port,
                "ble_address": connection.ble_address or "",
                "ble_pin": connection.ble_pin or "",
                "auto_reconnect": connection.auto_reconnect,
            },
            "bridge_channel": settings.meshcore.bridge_channel,
            "contact_name_overrides": dict(settings.meshcore.contact_name_overrides),
            "outbound_echo_text_fallback_enabled": settings.meshcore.outbound_echo_text_fallback_enabled,
            "outbound_echo_text_fallback_ttl_seconds": settings.meshcore.outbound_echo_text_fallback_ttl_seconds,
        },
        "web": {
            "enabled": web.enabled,
            "host": web.host,
            "port": web.port,
            "password": web.password,
            "title": web.title,
            "tile_url": web.tile_url,
            "tile_attribution": web.tile_attribution,
        },
        "chunking": {
            "enabled": chunking.enabled,
            "prefix_template": chunking.prefix_template,
            "max_chunk_bytes": chunking.max_chunk_bytes,
            "broadcast_max_chunk_bytes": chunking.broadcast_max_chunk_bytes,
            "payload_safety_margin_bytes": chunking.payload_safety_margin_bytes,
            "inter_chunk_delay_ms": chunking.inter_chunk_delay_ms,
            "broadcast_min_inter_chunk_delay_ms": chunking.broadcast_min_inter_chunk_delay_ms,
            "retry_max_attempts": chunking.retry_max_attempts,
            "retry_initial_delay_ms": chunking.retry_initial_delay_ms,
            "retry_backoff_factor": chunking.retry_backoff_factor,
            "abort_on_chunk_failure": chunking.abort_on_chunk_failure,
        },
        "runtime": {"log_level": settings.log_level},
    }


def _leaf_schemas(schema: Mapping[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Every setting by dotted path; objects with properties are sections, not settings."""
    result: dict[str, dict[str, Any]] = {}
    for key, child in (schema.get("properties") or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if child.get("type") == "object" and child.get("properties"):
            result.update(_leaf_schemas(child, path))
        else:
            result[path] = child
    return result


LEAVES = _leaf_schemas(SCHEMA)


def flatten(values: Mapping[str, Any]) -> dict[str, Any]:
    """Nested values (as ``core_values`` has them) by dotted path."""
    result: dict[str, Any] = {}
    for path in LEAVES:
        node: Any = values
        for key in path.split("."):
            node = node.get(key) if isinstance(node, Mapping) else None
        result[path] = node
    return result


def unflatten(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path, value in values.items():
        *parents, key = path.split(".")
        node = result
        for name in parents:
            node = node.setdefault(name, {})
        node[key] = copy.deepcopy(value)
    return result


def same(a: Any, b: Any) -> bool:
    """``==``, except True isn't 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def section_of(path: str) -> str:
    return path.split(".", 1)[0]


def serial_ports() -> list[str]:
    """Serial devices a radio might be on, for the control panel to suggest."""
    ports: list[str] = []
    for pattern in SERIAL_PORT_PATTERNS:
        ports.extend(sorted(glob.glob(pattern)))
    return ports


class SettingsOverrideStore:
    """Settings changed in the control panel, as JSON next to the other persistent state."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            LOGGER.error("Ignoring settings saved from the web app: can't read %s (%s)", self.path, exc)
            return {}
        settings = data.get("settings") if isinstance(data, dict) else None
        return dict(settings) if isinstance(settings, dict) else {}

    def save(self, overrides: Mapping[str, Any]) -> None:
        """Replace the file atomically; it may hold secrets, so only its owner can read it."""
        write_private_json(self.path, {"version": SETTINGS_VERSION, "settings": dict(overrides)})


class CoreSettings:
    """config.yaml's settings with the control panel's changes, and the changes themselves.

    ``settings`` is what Meshgram runs with: config.yaml and the saved changes.
    Changes made after that are saved, and run from the next start.
    """

    def __init__(
        self,
        config_data: Mapping[str, Any],
        *,
        config_path: str,
        store: Optional[SettingsOverrideStore] = None,
        check_telegram: Optional[TelegramCheck] = None,
    ):
        self.config_data = dict(config_data)
        self.config_path = config_path
        self.store = store
        self.check_telegram = check_telegram
        # Not strict: the token and group may come from the saved changes.
        self._base = flatten(core_values(build_settings(self.config_data, config_path=config_path, strict=False)))
        self._overrides = self._load_overrides()
        # Raises ConfigError when config.yaml and the saved changes together don't make a usable config.
        self.settings = build_settings(with_overrides(self.config_data, self._overrides), config_path=config_path)
        self._running = flatten(core_values(self.settings))

    def _load_overrides(self) -> dict[str, Any]:
        saved = self.store.load() if self.store is not None else {}
        overrides: dict[str, Any] = {}
        for path, value in saved.items():
            schema = LEAVES.get(path)
            if schema is None:
                LOGGER.warning("Ignoring saved setting %s: there's no such setting", path)
                continue
            try:
                validate(value, schema)
            except SettingsError as exc:
                LOGGER.warning("Ignoring saved setting %s: %s", path, exc)
                continue
            if not same(value, self._base[path]):
                overrides[path] = value
        if overrides:
            LOGGER.info(
                "Using settings saved from the web app over config.yaml: %s (reset them there to use config.yaml)",
                ", ".join(sorted(overrides)),
            )
        if overrides != saved and self.store is not None:
            # config.yaml has some of them now (copied from the export, say).
            LOGGER.info("Settings saved from the web app that config.yaml now has (or that can't be used): dropped")
            try:
                self.store.save(overrides)
            except OSError as exc:
                LOGGER.warning("Couldn't update %s: %s", self.store.path, exc)
        return overrides

    # --- Queries ------------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        """The settings as saved: config.yaml with the control panel's changes (secrets included)."""
        return unflatten({**self._base, **self._overrides})

    def overrides(self) -> dict[str, Any]:
        """The control panel's changes by dotted path, secrets included."""
        return copy.deepcopy(self._overrides)

    def pending(self) -> list[str]:
        """Settings saved since Meshgram started: they take effect when it restarts."""
        saved = {**self._base, **self._overrides}
        return [path for path in LEAVES if not same(saved[path], self._running[path])]

    def describe(self) -> dict[str, Any]:
        values = self.values()
        pending = self.pending()
        schema = copy.deepcopy(SCHEMA)
        ports = serial_ports()
        if ports:
            schema["properties"]["meshcore"]["properties"]["connection"]["properties"]["serial_device"]["examples"] = ports
        sections = []
        for key in SECTIONS:
            section_schema = schema["properties"][key]
            sections.append({
                "key": key,
                "title": section_schema.get("title", key),
                "description": section_schema.get("description", ""),
                "schema": section_schema,
                "values": mask_secrets(values[key], section_schema),
                "overridden": [path.split(".", 1)[1] for path in self._overrides if section_of(path) == key],
                "pending": [path.split(".", 1)[1] for path in pending if section_of(path) == key],
            })
        return {"sections": sections, "restart_needed": bool(pending)}

    # --- Changes ------------------------------------------------------------------

    def _section(self, key: str) -> str:
        if key not in SECTIONS:
            raise UnknownSectionError(key)
        return key

    async def update(self, key: str, values: Any) -> dict[str, Any]:
        """Replace one section's settings (as the control panel's form has them); saved, used after a restart.

        Raises ``SettingsError`` for values that don't fit (paths relative to the section).
        """
        key = self._section(key)
        if not isinstance(values, dict):
            raise SettingsError([("", "must be an object")])
        schema = SCHEMA["properties"][key]
        current = self.values()
        values = restore_secrets(values, current[key], schema)
        validate(values, schema)

        # Read the section the way config.yaml would be read, so defaults and
        # normalisation are the same.
        others = {path: value for path, value in self._overrides.items() if section_of(path) != key}
        data = with_overrides(self.config_data, others)
        data[key] = copy.deepcopy(values)
        try:
            read = core_values(build_settings(data, config_path=self.config_path, strict=False))
        except (ConfigError, ValueError) as exc:
            raise SettingsError([("", str(exc))]) from None
        section_values = {path: value for path, value in flatten(read).items() if section_of(path) == key}
        overrides = {
            **others,
            **{path: value for path, value in section_values.items() if not same(value, self._base[path])},
        }

        if key == "telegram" and self.check_telegram is not None:
            token, group_id = section_values["telegram.bot_token"], section_values["telegram.group_id"]
            if token != current["telegram"]["bot_token"] or group_id != current["telegram"]["group_id"]:
                await self.check_telegram(token, group_id)

        self._save(overrides)
        return self.describe()

    async def reset(self, key: str) -> dict[str, Any]:
        """Drop the control panel's changes to a section: back to config.yaml after a restart."""
        key = self._section(key)
        self._save({path: value for path, value in self._overrides.items() if section_of(path) != key})
        return self.describe()

    def _save(self, overrides: dict[str, Any]) -> None:
        if overrides == self._overrides:
            return
        if self.store is not None:
            # Raises OSError when it can't be saved; nothing has changed then.
            self.store.save(overrides)
        self._overrides = overrides
        LOGGER.info("Settings saved from the web app; they take effect when Meshgram restarts")


async def check_telegram(token: str, group_id: int, *, timeout: float = 15.0) -> None:
    """Ask Telegram whether the bot token works and the bot is in the group.

    Raises ``SettingsError`` when Telegram says no. When Telegram can't be
    reached the check is skipped: the settings may well be right.
    """
    from telegram import Bot
    from telegram.error import BadRequest, Forbidden, InvalidToken, TelegramError

    try:
        bot = Bot(token)
    except InvalidToken:
        raise SettingsError([("bot_token", "isn't a bot token")]) from None
    try:
        async with asyncio.timeout(timeout):
            async with bot:
                await bot.get_chat(group_id)
    except InvalidToken:
        raise SettingsError([("bot_token", "Telegram doesn't accept this token")]) from None
    except (BadRequest, Forbidden) as exc:
        raise SettingsError([("group_id", f"the bot can't reach this group ({exc.message})")]) from None
    except (TelegramError, TimeoutError, OSError) as exc:
        LOGGER.warning("Couldn't check the Telegram settings with Telegram (%s); saving them anyway", exc)

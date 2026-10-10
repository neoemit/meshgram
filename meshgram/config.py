"""Meshgram settings, read from a single YAML file (``config.yaml``).

Every setting, secrets included, lives in that file. The only environment
variables Meshgram reads say where files are, not how it behaves:

- ``MESHGRAM_CONFIG_PATH``: the config file (default ``config.yaml``).
- ``MESHGRAM_DATA_DIR``: where persistent state goes (see ``packet_map``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


MESHTASTIC_BACKEND = "meshtastic"
MESHCORE_BACKEND = "meshcore"
SUPPORTED_BACKENDS = {MESHTASTIC_BACKEND, MESHCORE_BACKEND}

MESHTASTIC_MODES = {"serial", "tcp"}
MESHCORE_MODES = {"serial", "tcp", "ble"}

CONFIG_PATH_ENV = "MESHGRAM_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config.yaml"
MIGRATION_HINT = (
    "Settings are no longer read from .env or environment variables; "
    "run `python -m meshgram.migrate_config` to move them into config.yaml"
)

# Environment variables that older versions read as settings. They're ignored
# now; ``meshgram.migrate_config`` knows where each one goes in config.yaml.
LEGACY_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_GROUP_ID",
    "LOG_LEVEL",
    "MESH_BACKEND",
    "MESH_MODE",
    "MESH_DEVICE",
    "MESH_BAUDRATE",
    "MESH_HOST",
    "MESH_PORT",
    "MESH_NO_NODES",
    "MESH_BLE_ADDRESS",
    "MESH_BLE_PIN",
    "MESH_AUTO_RECONNECT",
    "MESHMAPPER_IATA",
    "MESHMAPPER_PRIVATE_KEY",
    "MESHMAPPER_SUBSCRIBE_USERNAME",
    "MESHMAPPER_SUBSCRIBE_PASSWORD",
    "PACKET_MAP_HOST",
    "PACKET_MAP_PORT",
    "PACKET_MAP_PASSWORD",
    "PACKET_MAP_DB_PATH",
)


class ConfigError(ValueError):
    """The config file is missing or holds an invalid value."""


@dataclass(slots=True)
class MeshtasticConnectionConfig:
    mode: str = "serial"
    serial_device: str | None = None
    tcp_host: str = "localhost"
    tcp_port: int = 4403
    no_nodes: bool = False


@dataclass(slots=True)
class MeshtasticConfig:
    bridge_channel: int = 0
    node_name_overrides: dict[str, str] = field(default_factory=dict)
    connection: MeshtasticConnectionConfig = field(default_factory=MeshtasticConnectionConfig)


@dataclass(slots=True)
class MeshCoreConnectionConfig:
    mode: str = "serial"
    serial_device: str | None = None
    baudrate: int = 115200
    tcp_host: str = "localhost"
    tcp_port: int = 5000
    ble_address: str | None = None
    ble_pin: str | None = None
    auto_reconnect: bool = True


@dataclass(slots=True)
class MeshCoreConfig:
    bridge_channel: int = 0
    contact_name_overrides: dict[str, str] = field(default_factory=dict)
    outbound_echo_text_fallback_enabled: bool = False
    outbound_echo_text_fallback_ttl_seconds: float = 2.0
    connection: MeshCoreConnectionConfig = field(default_factory=MeshCoreConnectionConfig)


@dataclass(slots=True)
class MeshConfig:
    backend: str = MESHTASTIC_BACKEND


@dataclass(slots=True)
class TelegramConfig:
    include_captions: bool = True
    sender_prefix_template: str = "[{display_name}] {message}"


@dataclass(slots=True)
class ChunkingConfig:
    enabled: bool = True
    prefix_template: str = "({index}/{total}) "
    inter_chunk_delay_ms: int = 150
    max_chunk_bytes: int = 160
    broadcast_max_chunk_bytes: int = 120
    broadcast_min_inter_chunk_delay_ms: int = 2500
    retry_max_attempts: int = 3
    retry_initial_delay_ms: int = 500
    retry_backoff_factor: float = 2.0
    wait_for_ack: bool = True
    ack_timeout_ms: int = 20000
    abort_on_chunk_failure: bool = True
    payload_safety_margin_bytes: int = 12


@dataclass(slots=True)
class PluginConfig:
    name: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MeshgramSettings:
    telegram_bot_token: str
    telegram_group_id: int
    config_path: str
    log_level: str = "INFO"
    mesh: MeshConfig = field(default_factory=MeshConfig)
    meshtastic: MeshtasticConfig = field(default_factory=MeshtasticConfig)
    meshcore: MeshCoreConfig = field(default_factory=MeshCoreConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    plugins: list[PluginConfig] = field(default_factory=list)


def _default_plugins() -> list[PluginConfig]:
    return [
        PluginConfig(name="bridge", enabled=True, settings={}),
        PluginConfig(name="ping_pong", enabled=True, settings={}),
    ]


def _read_yaml(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to {path} and fill it in "
            f"(set {CONFIG_PATH_ENV} to use another path). Upgrading? {MIGRATION_HINT}."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a top-level mapping: {path}")
    return data


def _section(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return int(str(value))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    return float(str(value))


def _as_string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    result: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip()
        val = str(raw_val).strip()
        if not key or not val:
            continue
        result[key] = val
    return result


def _as_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _connection_mode(connection_data: dict[str, Any], *, section: str, allowed: set[str], active: bool) -> str:
    raw_mode = connection_data.get("mode", "serial")
    mode = str(raw_mode).strip().lower() or "serial"
    # Only the active backend's connection has to be usable.
    if active and mode not in allowed:
        raise ConfigError(f"{section}.connection.mode must be one of: {sorted(allowed)}; got {raw_mode!r}")
    return mode


def _build_meshtastic_config(config_data: dict[str, Any], *, backend: str) -> MeshtasticConfig:
    meshtastic_data = _section(config_data, "meshtastic")
    connection_data = _section(meshtastic_data, "connection")

    return MeshtasticConfig(
        bridge_channel=_as_int(meshtastic_data.get("bridge_channel"), 0),
        node_name_overrides=_as_string_dict(meshtastic_data.get("node_name_overrides")),
        connection=MeshtasticConnectionConfig(
            mode=_connection_mode(
                connection_data,
                section="meshtastic",
                allowed=MESHTASTIC_MODES,
                active=backend == MESHTASTIC_BACKEND,
            ),
            serial_device=_as_optional_string(connection_data.get("serial_device")),
            tcp_host=_as_optional_string(connection_data.get("tcp_host")) or "localhost",
            tcp_port=_as_int(connection_data.get("tcp_port"), 4403),
            no_nodes=_as_bool(connection_data.get("no_nodes"), False),
        ),
    )


def _build_meshcore_config(config_data: dict[str, Any], *, backend: str) -> MeshCoreConfig:
    meshcore_data = _section(config_data, "meshcore")
    connection_data = _section(meshcore_data, "connection")

    return MeshCoreConfig(
        bridge_channel=_as_int(meshcore_data.get("bridge_channel"), 0),
        contact_name_overrides=_as_string_dict(meshcore_data.get("contact_name_overrides")),
        outbound_echo_text_fallback_enabled=_as_bool(
            meshcore_data.get("outbound_echo_text_fallback_enabled"),
            False,
        ),
        outbound_echo_text_fallback_ttl_seconds=max(
            0.0,
            _as_float(meshcore_data.get("outbound_echo_text_fallback_ttl_seconds"), 2.0),
        ),
        connection=MeshCoreConnectionConfig(
            mode=_connection_mode(
                connection_data,
                section="meshcore",
                allowed=MESHCORE_MODES,
                active=backend == MESHCORE_BACKEND,
            ),
            serial_device=_as_optional_string(connection_data.get("serial_device")),
            baudrate=_as_int(connection_data.get("baudrate"), 115200),
            tcp_host=_as_optional_string(connection_data.get("tcp_host")) or "localhost",
            tcp_port=_as_int(connection_data.get("tcp_port"), 5000),
            ble_address=_as_optional_string(connection_data.get("ble_address")),
            ble_pin=_as_optional_string(connection_data.get("ble_pin")),
            auto_reconnect=_as_bool(connection_data.get("auto_reconnect"), True),
        ),
    )


def _build_plugins(plugins_data: Any) -> list[PluginConfig]:
    if not isinstance(plugins_data, list):
        return _default_plugins()

    plugins: list[PluginConfig] = []
    for item in plugins_data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        enabled = _as_bool(item.get("enabled"), True)
        settings = item.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        plugins.append(PluginConfig(name=name, enabled=enabled, settings=settings))
    return plugins or _default_plugins()


def legacy_env_vars(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names of set environment variables that used to configure Meshgram and are now ignored."""
    environ = os.environ if environ is None else environ
    return [name for name in LEGACY_ENV_VARS if name in environ]


def build_settings(config_data: dict[str, Any], *, config_path: str = DEFAULT_CONFIG_PATH) -> MeshgramSettings:
    """Validate parsed config data and turn it into settings."""
    runtime_data = _section(config_data, "runtime")
    telegram_data = _section(config_data, "telegram")
    chunking_data = _section(config_data, "chunking")
    mesh_data = _section(config_data, "mesh")

    token = _as_optional_string(telegram_data.get("bot_token"))
    if not token:
        raise ConfigError(f"telegram.bot_token is required in {config_path}")

    group_id_raw = _as_optional_string(telegram_data.get("group_id"))
    if group_id_raw is None:
        raise ConfigError(f"telegram.group_id is required in {config_path}")
    try:
        group_id = int(group_id_raw)
    except ValueError as exc:
        raise ConfigError(f"telegram.group_id must be an integer; got {group_id_raw!r}") from exc

    backend = str(mesh_data.get("backend", MESHTASTIC_BACKEND)).strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise ConfigError(f"mesh.backend must be one of: {sorted(SUPPORTED_BACKENDS)}; got {backend!r}")

    return MeshgramSettings(
        telegram_bot_token=token,
        telegram_group_id=group_id,
        config_path=config_path,
        log_level=str(runtime_data.get("log_level", "INFO")).strip().upper(),
        mesh=MeshConfig(backend=backend),
        meshtastic=_build_meshtastic_config(config_data, backend=backend),
        meshcore=_build_meshcore_config(config_data, backend=backend),
        telegram=TelegramConfig(
            include_captions=_as_bool(telegram_data.get("include_captions"), True),
            sender_prefix_template=str(
                telegram_data.get("sender_prefix_template", "[{display_name}] {message}")
            ),
        ),
        chunking=ChunkingConfig(
            enabled=_as_bool(chunking_data.get("enabled"), True),
            prefix_template=str(chunking_data.get("prefix_template", "({index}/{total}) ")),
            inter_chunk_delay_ms=max(0, _as_int(chunking_data.get("inter_chunk_delay_ms"), 150)),
            max_chunk_bytes=max(0, _as_int(chunking_data.get("max_chunk_bytes"), 160)),
            broadcast_max_chunk_bytes=max(0, _as_int(chunking_data.get("broadcast_max_chunk_bytes"), 120)),
            broadcast_min_inter_chunk_delay_ms=max(
                0,
                _as_int(chunking_data.get("broadcast_min_inter_chunk_delay_ms"), 2500),
            ),
            retry_max_attempts=max(1, _as_int(chunking_data.get("retry_max_attempts"), 3)),
            retry_initial_delay_ms=max(0, _as_int(chunking_data.get("retry_initial_delay_ms"), 500)),
            retry_backoff_factor=max(1.0, _as_float(chunking_data.get("retry_backoff_factor"), 2.0)),
            wait_for_ack=_as_bool(chunking_data.get("wait_for_ack"), True),
            ack_timeout_ms=max(1000, _as_int(chunking_data.get("ack_timeout_ms"), 20000)),
            abort_on_chunk_failure=_as_bool(chunking_data.get("abort_on_chunk_failure"), True),
            payload_safety_margin_bytes=max(0, _as_int(chunking_data.get("payload_safety_margin_bytes"), 12)),
        ),
        plugins=_build_plugins(config_data.get("plugins")),
    )


def load_settings(config_path: str | None = None) -> MeshgramSettings:
    """Read settings from ``config_path`` (default: ``$MESHGRAM_CONFIG_PATH`` or ``config.yaml``)."""
    if config_path is None:
        config_path = os.getenv(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH
    return build_settings(_read_yaml(config_path), config_path=config_path)

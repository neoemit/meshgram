from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


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
class TelegramConfig:
    include_captions: bool = True
    sender_prefix_template: str = "[{display_name}] {message}"


@dataclass(slots=True)
class ChunkingConfig:
    enabled: bool = True
    prefix_template: str = "({index}/{total}) "
    inter_chunk_delay_ms: int = 150
    retry_max_attempts: int = 3
    retry_initial_delay_ms: int = 500
    retry_backoff_factor: float = 2.0
    abort_on_chunk_failure: bool = True
    payload_safety_margin_bytes: int = 24


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
    meshtastic: MeshtasticConfig = field(default_factory=MeshtasticConfig)
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
        return {}

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a top-level mapping: {path}")
    return data


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


def load_settings() -> MeshgramSettings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    group_id_raw = os.getenv("TELEGRAM_GROUP_ID")

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not group_id_raw:
        raise ValueError("TELEGRAM_GROUP_ID is required")

    try:
        group_id = int(group_id_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_GROUP_ID must be an integer") from exc

    config_path = os.getenv("MESHGRAM_CONFIG_PATH", "config.yaml")
    config_data = _read_yaml(config_path)

    runtime_data = config_data.get("runtime", {}) if isinstance(config_data.get("runtime", {}), dict) else {}
    meshtastic_data = (
        config_data.get("meshtastic", {}) if isinstance(config_data.get("meshtastic", {}), dict) else {}
    )
    telegram_data = config_data.get("telegram", {}) if isinstance(config_data.get("telegram", {}), dict) else {}
    chunking_data = config_data.get("chunking", {}) if isinstance(config_data.get("chunking", {}), dict) else {}

    connection_data = (
        meshtastic_data.get("connection", {})
        if isinstance(meshtastic_data.get("connection", {}), dict)
        else {}
    )

    mode = str(os.getenv("MESH_MODE", connection_data.get("mode", "serial"))).strip().lower()
    if mode not in {"serial", "tcp"}:
        raise ValueError("Meshtastic mode must be one of: serial, tcp")

    serial_device = os.getenv("MESH_DEVICE", connection_data.get("serial_device"))
    tcp_host = os.getenv("MESH_HOST", str(connection_data.get("tcp_host", "localhost")))

    tcp_port_raw = os.getenv("MESH_PORT")
    if tcp_port_raw is not None:
        tcp_port = _as_int(tcp_port_raw, 4403)
    else:
        tcp_port = _as_int(connection_data.get("tcp_port"), 4403)

    no_nodes = _as_bool(os.getenv("MESH_NO_NODES"), _as_bool(connection_data.get("no_nodes"), False))

    plugins_data = config_data.get("plugins")
    if isinstance(plugins_data, list):
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
        if not plugins:
            plugins = _default_plugins()
    else:
        plugins = _default_plugins()

    log_level = str(os.getenv("LOG_LEVEL", runtime_data.get("log_level", "INFO"))).upper()

    return MeshgramSettings(
        telegram_bot_token=token,
        telegram_group_id=group_id,
        config_path=config_path,
        log_level=log_level,
        meshtastic=MeshtasticConfig(
            bridge_channel=_as_int(meshtastic_data.get("bridge_channel"), 0),
            node_name_overrides=_as_string_dict(meshtastic_data.get("node_name_overrides")),
            connection=MeshtasticConnectionConfig(
                mode=mode,
                serial_device=serial_device,
                tcp_host=tcp_host,
                tcp_port=tcp_port,
                no_nodes=no_nodes,
            ),
        ),
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
            retry_max_attempts=max(1, _as_int(chunking_data.get("retry_max_attempts"), 3)),
            retry_initial_delay_ms=max(0, _as_int(chunking_data.get("retry_initial_delay_ms"), 500)),
            retry_backoff_factor=max(1.0, _as_float(chunking_data.get("retry_backoff_factor"), 2.0)),
            abort_on_chunk_failure=_as_bool(chunking_data.get("abort_on_chunk_failure"), True),
            payload_safety_margin_bytes=max(0, _as_int(chunking_data.get("payload_safety_margin_bytes"), 24)),
        ),
        plugins=plugins,
    )

"""MeshRank (https://meshrank.net) observer: uploads the packets the radio hears to MeshRank.

MeshRank's broker (``meshrank.net:8883``, MQTT over TLS) takes anonymous observers,
known by the public key in their topics, as in its meshcoretomqtt preset: data
starts flowing with no account. To attach the observer to a MeshRank account, set
the login from MeshRank's observer registration (``username`` / ``password``); the
observer keeps its public-key identity. Status documents aren't retained there.
See ``mqtt_observer`` for the protocol.
"""
from __future__ import annotations

from typing import Any

from .mqtt_observer import (
    REGION_SETTING,
    STATUS_INTERVAL_SETTING,
    SUBSCRIBER_SETTINGS,
    MqttObserverPlugin,
    ObserverConfig,
)

NETWORK = "MeshRank"
DEFAULT_SERVER = "meshrank.net"
PRESET: dict[str, Any] = {
    "label": NETWORK,
    "network": NETWORK,
    "plugin": "meshrank",
    "server": DEFAULT_SERVER,
    "port": 8883,
    "transport": "tcp",
    "tls": True,
    "auth": "none",
    "retain_status": False,
    "status_publish": "meshrank_publish",
    "status_subscribe": "meshrank_subscribe",
}


class MeshRankPlugin(MqttObserverPlugin):
    name = "meshrank"
    title = "MeshRank observer"
    description = (
        "Uploads every packet the radio hears to MeshRank (meshrank.net) as an observer. "
        "No account needed; a registration login attaches it to your MeshRank account."
    )
    settings_schema = {
        "type": "object",
        "required": ["iata"],
        "properties": {
            "iata": REGION_SETTING,
            "username": {
                "type": "string",
                "title": "Account login",
                "description": "Optional: from MeshRank's observer registration (Account → Observer Setup).",
            },
            "password": {"type": "string", "writeOnly": True, "title": "Account password"},
            **SUBSCRIBER_SETTINGS,
            "server": {"type": "string", "minLength": 1, "title": "MQTT server", "default": DEFAULT_SERVER},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "title": "MQTT port", "default": 8883},
            "transport": {"type": "string", "enum": ["tcp", "websockets"], "title": "MQTT transport", "default": "tcp"},
            "status_interval_seconds": STATUS_INTERVAL_SETTING,
        },
    }

    def build_configs(self, settings: dict[str, Any]) -> list[ObserverConfig]:
        return [ObserverConfig.from_settings(settings, PRESET)]

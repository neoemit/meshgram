"""MeshRank (https://meshrank.net) observer: uploads the packets the radio hears to MeshRank.

MeshRank's broker (``meshrank.net:8883``, MQTT over TLS) takes anonymous observers,
known by the public key in their topics (``meshcore/<IATA>/<PUBLIC_KEY>/…``), as in
its meshcoretomqtt preset: data starts flowing with no account. To attach the
observer to a MeshRank account, create a key under Account → Observer Setup → MQTT
observer registration and set it as ``registration_key``: the observer then uploads
to ``meshrank/uplink/<KEY>/<PUBLIC_KEY>/…`` (still anonymous, the key is in the
topic), as meshcore-packet-capture's preset and MeshRank's generated override do,
and keeps its public-key identity. Status documents aren't retained there.

MeshRank's broker grants any subscription but delivers other observers' packets
only to accounts its operator allows, so reading them takes a subscriber account
from MeshRank. See ``mqtt_observer`` for the protocol.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .mqtt_observer import (
    REGION_SETTING,
    STATUS_INTERVAL_SETTING,
    SUBSCRIBER_SETTINGS,
    MqttObserverPlugin,
    ObserverConfig,
)

LOGGER = logging.getLogger(__name__)

NETWORK = "MeshRank"
DEFAULT_SERVER = "meshrank.net"
REGISTERED_TOPIC_STATUS = "meshrank/uplink/{TOKEN}/{PUBLIC_KEY}/status"
REGISTERED_TOPIC_PACKETS = "meshrank/uplink/{TOKEN}/{PUBLIC_KEY}/packets"
SUBSCRIBER_HINT = (
    "MeshRank uplinks can only publish (its broker accepts subscriptions but sends them nothing); "
    "reading other observers' packets takes an account from MeshRank's operator."
)
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
    "subscriber_hint": SUBSCRIBER_HINT,
}

# The key as pasted from MeshRank's generated config: the bare key, or a topic with it.
_UPLINK_TOPIC_RE = re.compile(r"meshrank/uplink/([^/\s]+)/")


def registration_key(value: Any) -> str:
    """The registration key from ``value``: the key itself, or a ``meshrank/uplink/<KEY>/…`` topic."""
    text = str(value or "").strip().strip("\"'")
    match = _UPLINK_TOPIC_RE.search(text)
    return match.group(1) if match else text


class MeshRankPlugin(MqttObserverPlugin):
    name = "meshrank"
    title = "MeshRank observer"
    description = (
        "Uploads every packet the radio hears to MeshRank (meshrank.net) as an observer. "
        "No account needed; a registration key attaches it to your MeshRank account."
    )
    settings_schema = {
        "type": "object",
        "required": ["iata"],
        "properties": {
            "iata": REGION_SETTING,
            "registration_key": {
                "type": "string",
                "writeOnly": True,
                "title": "Registration key",
                "description": (
                    "Optional: from meshrank.net, Account → Observer Setup → MQTT observer registration "
                    "(the key, or the packets topic it's in). Attaches the observer to your account."
                ),
                "pattern": r"^\S*$",
                "x-pattern-message": "must not contain spaces",
            },
            **SUBSCRIBER_SETTINGS,
            "server": {"type": "string", "minLength": 1, "title": "MQTT server", "default": DEFAULT_SERVER},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "title": "MQTT port", "default": 8883},
            "transport": {"type": "string", "enum": ["tcp", "websockets"], "title": "MQTT transport", "default": "tcp"},
            "status_interval_seconds": STATUS_INTERVAL_SETTING,
        },
    }

    def build_configs(self, settings: dict[str, Any]) -> list[ObserverConfig]:
        preset = dict(PRESET)
        key = registration_key(settings.get("registration_key"))
        if key:
            preset.update(topic_token=key, topic_status=REGISTERED_TOPIC_STATUS, topic_packets=REGISTERED_TOPIC_PACKETS)
        if settings.get("username") or settings.get("password"):
            # Earlier versions took a "registration login"; MeshRank registers by key.
            LOGGER.warning(
                "MeshRank: username/password aren't used (MeshRank's broker is anonymous). To attach the "
                "observer to your account, set registration_key from Account → Observer Setup on meshrank.net"
            )
        settings = {name: value for name, value in settings.items() if name not in ("username", "password", "topic_token")}
        return [ObserverConfig.from_settings(settings, preset)]

"""LetsMesh (https://letsmesh.net) observer: uploads the packets the radio hears to its packet analyzer.

LetsMesh runs two MQTT brokers, US and EU, both on WebSockets + TLS (port 443) with
MeshCore's device-signed login (``v1_<PUBLIC_KEY>`` and an Ed25519 JWT whose ``aud``
is the broker), as in meshcoretomqtt's LetsMesh preset. The plugin uploads to the
ones picked in ``servers`` (both by default); see ``mqtt_observer`` for the protocol.

The observer is attached to a LetsMesh account the way meshcore-packet-capture and
meshcoretomqtt do it: the token carries the account's email (``owner_email``, the
address used to log in to LetsMesh) and optionally the owner's companion public key
(``owner_public_key``). LetsMesh's brokers (meshcore-mqtt-broker) only let
operator-issued subscriber accounts read: other observers' packets need one
(``subscribe_username`` / ``subscribe_password``).
"""
from __future__ import annotations

from typing import Any

from .mqtt_observer import (
    OWNER_SETTINGS,
    PRIVATE_KEY_SETTING,
    REGION_SETTING,
    STATUS_INTERVAL_SETTING,
    SUBSCRIBER_SETTINGS,
    MqttObserverPlugin,
    ObserverConfig,
)

NETWORK = "LetsMesh"
SERVERS: dict[str, dict[str, Any]] = {
    "us": {"label": "LetsMesh US", "server": "mqtt-us-v1.letsmesh.net", "status_publish": "letsmesh_us_publish"},
    "eu": {"label": "LetsMesh EU", "server": "mqtt-eu-v1.letsmesh.net", "status_publish": "letsmesh_eu_publish"},
}
STATUS_SUBSCRIBE = "letsmesh_subscribe"
SUBSCRIBER_HINT = (
    "LetsMesh observer logins can only publish; reading other observers' packets takes a subscriber "
    "account the LetsMesh team creates."
)


def preset(region: str) -> dict[str, Any]:
    server = SERVERS[region]
    return {
        "network": NETWORK,
        "plugin": "letsmesh",
        "port": 443,
        "transport": "websockets",
        "tls": True,
        "auth": "token",
        "status_subscribe": STATUS_SUBSCRIBE,
        "subscriber_hint": SUBSCRIBER_HINT,
        **server,
    }


def selected_servers(value: Any) -> list[str]:
    """The servers to upload to, in ``SERVERS`` order; unset means all of them."""
    if value is None:
        return list(SERVERS)
    picked = {str(item).strip().lower() for item in (value if isinstance(value, (list, tuple)) else [value])}
    return [region for region in SERVERS if region in picked]


class LetsMeshPlugin(MqttObserverPlugin):
    name = "letsmesh"
    title = "LetsMesh observer"
    description = (
        "Uploads every packet the radio hears to LetsMesh's packet analyzer (letsmesh.net) as an observer, "
        "on its US and/or EU MQTT servers. Your LetsMesh email attaches it to your account."
    )
    settings_schema = {
        "type": "object",
        "required": ["iata"],
        "properties": {
            "iata": REGION_SETTING,
            "servers": {
                "type": "array",
                "title": "Servers",
                "description": "LetsMesh's MQTT servers to upload to.",
                "items": {
                    "type": "string",
                    "enum": list(SERVERS),
                    "x-enum-titles": [f"{server['label']} ({server['server']})" for server in SERVERS.values()],
                },
                "uniqueItems": True,
                "minItems": 1,
                "default": list(SERVERS),
            },
            **OWNER_SETTINGS,
            **SUBSCRIBER_SETTINGS,
            "status_interval_seconds": STATUS_INTERVAL_SETTING,
            "private_key": PRIVATE_KEY_SETTING,
        },
    }

    def build_configs(self, settings: dict[str, Any]) -> list[ObserverConfig]:
        return [ObserverConfig.from_settings(settings, preset(region)) for region in selected_servers(settings.get("servers"))]

    def no_server_reason(self) -> str:
        return "pick at least one server (settings.servers: us, eu)"

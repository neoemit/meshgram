"""Custom MQTT servers: the observer uploads (and other observers' packets) on brokers of your choice.

For brokers that aren't one of the packet analyzers with their own plugin (a club's or
a region's broker, a self-hosted one): ``servers`` names any number of them, each with
its own address, transport and login. They speak the same observer wire protocol as the
analyzers (``mqtt_observer``): every packet the radio hears goes to
``meshcore/<IATA>/<PUBLIC_KEY>/packets``, with an online/offline document at ``…/status``.

Logins: ``password`` (a username and password from the broker's operator, the usual
case), ``none`` (anonymous) or ``token`` (MeshCore's device-signed login, as on
meshcore-mqtt-broker). Unset, it's ``password`` when a username is set, else ``none``.

Each server also reads the region's ``packets`` topics (``subscribe``), with the same
login unless a separate subscriber account is set; an anonymous server reads
anonymously. Device-signed logins can't read on meshcore-mqtt-broker, so they need a
subscriber account. The packets are stamped with the server's name as their network.
"""
from __future__ import annotations

import re
from typing import Any

from .mqtt_observer import (
    AUTH_METHODS,
    AUTH_NONE,
    AUTH_PASSWORD,
    AUTH_TOKEN,
    DEFAULT_TOPIC_PACKETS,
    DEFAULT_TOPIC_STATUS,
    DEFAULT_TOPIC_SUBSCRIBE,
    PRIVATE_KEY_SETTING,
    REGION_SETTING,
    STATUS_INTERVAL_SETTING,
    MqttObserverPlugin,
    ObserverConfig,
    _as_bool,
    _transport,
)

# Settings of the plugin every server uses (a server's own settings win).
SHARED_SETTINGS = ("iata", "status_interval_seconds", "private_key")
SUBSCRIBER_HINT = "Device-signed logins usually can only publish; the broker's operator can give you an account that reads."

SERVER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["server"],
    "properties": {
        "server": {"type": "string", "minLength": 1, "title": "MQTT server", "description": "Host name or IP address."},
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "title": "Port",
            "description": "Empty: 1883, or 8883 with TLS (WebSockets: 80, or 443 with TLS).",
            "default": 1883,
        },
        "transport": {
            "type": "string",
            "enum": ["tcp", "websockets"],
            "x-enum-titles": ["TCP", "WebSockets"],
            "title": "Transport",
            "default": "tcp",
        },
        "tls": {"type": "boolean", "title": "TLS", "default": False},
        "tls_verify": {"type": "boolean", "title": "Check the TLS certificate", "default": True},
        "websocket_path": {"type": "string", "title": "WebSocket path", "description": "WebSockets only.", "default": "/"},
        "auth": {
            "type": "string",
            "enum": list(AUTH_METHODS),
            "x-enum-titles": ["Token signed by the radio", "Username and password", "Anonymous"],
            "title": "Login",
            "description": "Empty: username and password if a username is set, otherwise anonymous.",
        },
        "username": {"type": "string", "title": "Username"},
        "password": {"type": "string", "writeOnly": True, "title": "Password"},
        "subscribe": {
            "type": "boolean",
            "title": "Receive other observers' packets",
            "description": "Subscribes to the region's packets with the same login (or the subscriber account below).",
            "default": True,
        },
        "subscribe_username": {"type": "string", "title": "Subscriber account", "description": "Only if reading needs another login."},
        "subscribe_password": {"type": "string", "writeOnly": True, "title": "Subscriber password"},
        "topic_packets": {
            "type": "string",
            "title": "Packets topic",
            "description": "{IATA}: the region code, {PUBLIC_KEY}: the radio's public key.",
            "default": DEFAULT_TOPIC_PACKETS,
        },
        "topic_status": {"type": "string", "title": "Status topic", "default": DEFAULT_TOPIC_STATUS},
        "topic_subscribe": {
            "type": "string",
            "title": "Topic to read other observers' packets from",
            "default": DEFAULT_TOPIC_SUBSCRIBE,
        },
        "retain_status": {"type": "boolean", "title": "Retain the status document", "default": True},
    },
}


def default_port(transport: str, tls: bool) -> int:
    if transport == "websockets":
        return 443 if tls else 80
    return 8883 if tls else 1883


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "server"


class CustomMqttPlugin(MqttObserverPlugin):
    name = "custom_mqtt"
    title = "Custom MQTT servers"
    description = (
        "Uploads every packet the radio hears to MQTT brokers you choose (a club's or a region's, a self-hosted "
        "one) as an observer, and receives the packets other observers send there."
    )
    settings_schema = {
        "type": "object",
        "required": ["iata"],
        "properties": {
            "iata": REGION_SETTING,
            "servers": {
                "type": "object",
                "title": "Servers",
                "description": "Each server's name tags the packets received from it.",
                "x-key-title": "Name",
                "additionalProperties": SERVER_SCHEMA,
            },
            "status_interval_seconds": STATUS_INTERVAL_SETTING,
            "private_key": PRIVATE_KEY_SETTING,
        },
    }

    def build_configs(self, settings: dict[str, Any]) -> list[ObserverConfig]:
        servers = settings.get("servers")
        if not isinstance(servers, dict):
            return []
        shared = {key: settings[key] for key in SHARED_SETTINGS if key in settings}
        configs = []
        slugs: set[str] = set()
        for name, server in servers.items():
            label = str(name).strip()
            if not label or not isinstance(server, dict):
                continue
            slug = base = _slug(label)
            count = 1
            while slug in slugs:
                count += 1
                slug = f"{base}_{count}"
            slugs.add(slug)
            configs.append(ObserverConfig.from_settings({**shared, **server}, self._preset(label, slug, server)))
        return configs

    def _preset(self, label: str, slug: str, server: dict[str, Any]) -> dict[str, Any]:
        username = str(server.get("username") or "").strip()
        auth = str(server.get("auth") or "").strip().lower() or AUTH_NONE
        if auth not in AUTH_METHODS:
            auth = AUTH_TOKEN
        if auth == AUTH_NONE and username:
            auth = AUTH_PASSWORD
        transport = _transport(server.get("transport"), "tcp")
        tls = _as_bool(server.get("tls"), False)
        preset: dict[str, Any] = {
            "label": label,
            "network": label,
            "plugin": self.name,
            "port": default_port(transport, tls),
            "transport": transport,
            "tls": tls,
            "auth": auth,
            "subscribe_anonymous": auth == AUTH_NONE,
            "subscriber_hint": SUBSCRIBER_HINT,
            "status_publish": f"mqtt_custom_{slug}_publish",
            "status_subscribe": f"mqtt_custom_{slug}_subscribe",
        }
        if auth == AUTH_PASSWORD and not server.get("subscribe_username"):
            # Reads with the same login.
            preset.update(subscribe_username=username, subscribe_password=server.get("password") or "")
        return preset

    def subscriber_configs(self) -> list[ObserverConfig]:
        return self.configs

    def no_server_reason(self) -> str:
        return "add a server (settings.servers)"

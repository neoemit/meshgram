"""Upload received MeshCore packets to MeshMapper (https://meshmapper.net) over MQTT.

This turns the MeshCore companion radio connected to Meshgram into a MeshMapper
*observer*. It speaks the same wire protocol as the observers listed in the
MeshMapper wiki (meshcoretomqtt, meshcore-ha, pyMC): every RF packet the radio
hears is published as JSON to ``meshcore/<IATA>/<PUBLIC_KEY>/packets`` and a
retained online/offline document is kept at ``meshcore/<IATA>/<PUBLIC_KEY>/status``.

Authentication uses a MeshCore auth token: an Ed25519-signed JWT whose
``publicKey`` claim is the radio's public key. By default the radio signs the
token itself (no private key ever leaves the device); a private key can be
configured as an alternative.

Device-signed accounts are publish-only: MeshMapper's broker
(meshcore-mqtt-broker) closes the connection of a device-authenticated client
that tries to subscribe, and only accounts the broker operator issues may read.
The packets *other* observers upload are therefore received from MeshMapper's
public live feed (the websocket behind its maps' "Visualize Live" view), which
needs no account but carries packet metadata rather than packet bytes. With a
subscriber account (username/password issued by the broker operator) the plugin
also opens a second, read-only MQTT connection to the region's ``packets``
topics, which carries the full packets; the live feed stands by while it is
subscribed. Either way the packets go to the transport's remote RX log
listeners (the packet_map plugin shows them).

The plugin never emits bridge actions and only listens to raw RF logs, so it
does not interact with the other plugins.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import random
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from meshgram import __version__
from meshgram._ed25519 import public_key_from_expanded, sign_with_expanded_key
from meshgram.config import MESHCORE_BACKEND
from meshgram.meshcore_packets import (
    DIRECT_ROUTE_TYPES,
    PAYLOAD_TYPE_NAMES,
    PAYLOAD_TYPE_TRACE,
    ROUTE_TYPE_NAMES,
    is_valid_position,
    packet_hash,
    parse_packet,
)
from meshgram.plugin import BasePlugin
from meshgram.status import CONNECTED, CONNECTING, DISABLED, DISCONNECTED, StatusRegistry
from meshgram.types import PluginAction, PluginContext

LOGGER = logging.getLogger(__name__)

DEFAULT_SERVER = "mqtt.meshmapper.net"
DEFAULT_TOPIC_STATUS = "meshcore/{IATA}/{PUBLIC_KEY}/status"
DEFAULT_TOPIC_PACKETS = "meshcore/{IATA}/{PUBLIC_KEY}/packets"
DEFAULT_TOPIC_SUBSCRIBE = "meshcore/{IATA}/+/packets"
DEFAULT_LIVE_FEED_URL = "wss://analyzer.meshmapper.net/ws"
PLACEHOLDER_IATA_CODES = {"", "XXX", "XYZ"}
CLIENT_VERSION = f"meshgram/{__version__}"

MQTT_AUTH_FAILURE_CODES = {4, 5, 134, 135}

# Live feed client rules, the same as MeshMapper's own web client follows.
FEED_PING_SECONDS = 30.0
FEED_SILENCE_SECONDS = 65.0  # nothing (event or pong) for this long: the link is half-open
FEED_BACKOFF_BASE_SECONDS = 1.0
FEED_BACKOFF_MAX_SECONDS = 30.0
FEED_BACKOFF_CAP_ATTEMPT = 5
FEED_STABLE_SECONDS = 10.0  # only a connection that held this long resets the backoff
FEED_SHED_LOAD_CLOSE_CODES = {1008, 1013}  # the feed is shedding load: back off the longest

STATUS_PUBLISH = "mqtt_publish"
STATUS_SUBSCRIBE = "mqtt_subscribe"
STATUS_FEED = "meshmapper_feed"
STATUS_LABELS = {
    STATUS_PUBLISH: "MeshMapper MQTT (publish)",
    STATUS_SUBSCRIBE: "MeshMapper MQTT (subscribe)",
    STATUS_FEED: "MeshMapper live feed",
}

HEX_RE = re.compile(r"^[0-9a-fA-F]*$")


# --- Settings ----------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_hex(value: Any) -> str:
    return "".join(str(value or "").split()).upper()


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class MeshMapperConfig:
    iata: str
    server: str = DEFAULT_SERVER
    port: int = 443
    transport: str = "websockets"
    websocket_path: str = "/"
    tls: bool = True
    tls_verify: bool = True
    keepalive: int = 60
    token_audience: str = DEFAULT_SERVER
    token_ttl_seconds: int = 3600
    status_interval_seconds: int = 300
    client_id_prefix: str = "meshgram_"
    topic_status: str = DEFAULT_TOPIC_STATUS
    topic_packets: str = DEFAULT_TOPIC_PACKETS
    subscribe: bool = True
    topic_subscribe: str = DEFAULT_TOPIC_SUBSCRIBE
    # Subscriber account for other observers' packets (device-signed accounts can't subscribe).
    subscribe_username: str = ""
    subscribe_password: str = ""
    subscribe_server: str = DEFAULT_SERVER
    subscribe_port: int = 443
    subscribe_transport: str = "websockets"
    subscribe_tls: bool = True
    # Public, account-free feed of every observer's packets (metadata only).
    live_feed: bool = True
    live_feed_url: str = DEFAULT_LIVE_FEED_URL
    private_key: str = ""

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "MeshMapperConfig":
        iata = os.getenv("MESHMAPPER_IATA") or settings.get("iata") or ""
        private_key = os.getenv("MESHMAPPER_PRIVATE_KEY") or settings.get("private_key") or ""
        server = str(settings.get("server") or DEFAULT_SERVER).strip()
        transport = str(settings.get("transport") or "websockets").strip().lower()
        transport = transport if transport in {"websockets", "tcp"} else "websockets"
        port = _as_int(settings.get("port"), 443)
        tls = _as_bool(settings.get("tls"), True)
        subscribe_transport = str(settings.get("subscribe_transport") or transport).strip().lower()
        subscribe_username = os.getenv("MESHMAPPER_SUBSCRIBE_USERNAME") or settings.get("subscribe_username") or ""
        subscribe_password = os.getenv("MESHMAPPER_SUBSCRIBE_PASSWORD") or settings.get("subscribe_password") or ""
        return cls(
            iata=str(iata).strip().upper(),
            server=server,
            port=port,
            transport=transport,
            websocket_path=str(settings.get("websocket_path") or "/"),
            tls=tls,
            tls_verify=_as_bool(settings.get("tls_verify"), True),
            keepalive=max(10, _as_int(settings.get("keepalive"), 60)),
            token_audience=str(settings.get("token_audience", server) or "").strip(),
            token_ttl_seconds=max(600, _as_int(settings.get("token_ttl_seconds"), 3600)),
            status_interval_seconds=max(30, _as_int(settings.get("status_interval_seconds"), 300)),
            client_id_prefix=str(settings.get("client_id_prefix") or "meshgram_"),
            topic_status=str(settings.get("topic_status") or DEFAULT_TOPIC_STATUS),
            topic_packets=str(settings.get("topic_packets") or DEFAULT_TOPIC_PACKETS),
            subscribe=_as_bool(settings.get("subscribe"), True),
            topic_subscribe=str(settings.get("topic_subscribe") or DEFAULT_TOPIC_SUBSCRIBE),
            subscribe_username=str(subscribe_username).strip(),
            subscribe_password=str(subscribe_password),
            subscribe_server=str(settings.get("subscribe_server") or server).strip(),
            subscribe_port=_as_int(settings.get("subscribe_port"), port),
            subscribe_transport=subscribe_transport if subscribe_transport in {"websockets", "tcp"} else transport,
            subscribe_tls=_as_bool(settings.get("subscribe_tls"), tls),
            live_feed=_as_bool(settings.get("live_feed"), True),
            live_feed_url=str(settings.get("live_feed_url") or DEFAULT_LIVE_FEED_URL).strip(),
            private_key=_clean_hex(private_key),
        )

    def validation_error(self) -> Optional[str]:
        if self.iata in PLACEHOLDER_IATA_CODES:
            return "set plugins[meshmapper].settings.iata (or MESHMAPPER_IATA) to your MeshMapper region code"
        if not self.server:
            return "settings.server must not be empty"
        if self.private_key and (len(self.private_key) != 128 or not HEX_RE.match(self.private_key)):
            return "private_key must be 128 hex characters (64-byte MeshCore private key)"
        return None

    def subscriber_disabled_reason(self) -> Optional[str]:
        """Why other observers' packets won't be received, or ``None`` if the subscriber should run."""
        if not self.subscribe:
            return "Turned off (subscribe: false)"
        if not self.subscribe_username or not self.subscribe_password:
            return (
                "Needs a subscriber account: device-signed observers can only publish. "
                "Set MESHMAPPER_SUBSCRIBE_USERNAME and MESHMAPPER_SUBSCRIBE_PASSWORD"
            )
        return None

    def live_feed_disabled_reason(self) -> Optional[str]:
        """Why the live feed won't run, or ``None`` if it should."""
        if not self.subscribe:
            return "Turned off (subscribe: false)"
        if not self.live_feed or not self.live_feed_url:
            return "Turned off (live_feed: false)"
        return None


# --- Packet formatting -------------------------------------------------------


def build_packet_message(
    rx_log: dict[str, Any],
    origin: str,
    origin_id: str,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Convert a meshcore_py RX_LOG_DATA payload into the observer ``packets`` JSON.

    Field names and string encoding follow meshcoretomqtt / meshcore-packet-capture
    so MeshMapper's ingest can process the packet like any other observer's.
    """
    raw_hex = str(rx_log.get("payload") or "").strip()
    if not raw_hex:
        # ``raw_hex`` is the whole log frame: SNR byte, RSSI byte, then the packet.
        raw_hex = str(rx_log.get("raw_hex") or "").strip()[4:]
    if not raw_hex or len(raw_hex) % 2 or not HEX_RE.match(raw_hex):
        return None

    raw = bytes.fromhex(raw_hex)
    parsed = parse_packet(raw)
    if parsed is None:
        return None

    now = now or datetime.now(timezone.utc)
    route = "D" if parsed["route_type"] in DIRECT_ROUTE_TYPES else "F"
    message: dict[str, Any] = {
        "origin": origin,
        "origin_id": origin_id,
        "timestamp": now.isoformat(),
        "type": "PACKET",
        "direction": "rx",
        "time": now.strftime("%H:%M:%S"),
        "date": f"{now.day}/{now.month}/{now.year}",
        "len": str(len(raw)),
        "packet_type": str(parsed["payload_type"]),
        "route": route,
        "payload_len": str(len(parsed["payload"])),
        "raw": raw_hex.upper(),
        "hash": packet_hash(parsed["payload_type"], parsed["path_len_byte"], parsed["payload"]),
    }
    for source_key, target_key in (("snr", "SNR"), ("rssi", "RSSI")):
        if rx_log.get(source_key) is not None:
            message[target_key] = str(rx_log[source_key])
    if route == "D" and parsed["path_hashes"]:
        message["path"] = ",".join(parsed["path_hashes"])
    return message


def parse_observer_packet(topic: str, payload: bytes, own_public_key: str) -> Optional[dict[str, Any]]:
    """Turn another observer's ``packets`` message into an RF-log-like dict, or ``None`` to skip it.

    Skips our own uploads, non-packet documents and packets the observer transmitted itself.
    """
    parts = topic.split("/")
    observer_id = _clean_hex(parts[-2]) if len(parts) >= 2 else ""
    try:
        message = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    if not HEX_RE.match(observer_id) or observer_id in {"", "+"}:
        observer_id = _clean_hex(message.get("origin_id"))
    if not observer_id or not HEX_RE.match(observer_id) or observer_id == own_public_key:
        return None
    if str(message.get("type") or "PACKET").upper() != "PACKET" or str(message.get("direction") or "rx").lower() != "rx":
        return None
    raw_hex = _clean_hex(message.get("raw"))
    if not raw_hex or len(raw_hex) % 2 or not HEX_RE.match(raw_hex):
        return None
    observation: dict[str, Any] = {
        "payload": raw_hex,
        "observer_id": observer_id,
        "observer_name": str(message.get("origin") or "").strip() or None,
    }
    for source_key, target_key in (("SNR", "snr"), ("RSSI", "rssi")):
        value = _as_float(message.get(source_key))
        if value is not None:
            observation[target_key] = value
    return observation


def parse_feed_observation(data: Any, own_public_key: str) -> Optional[dict[str, Any]]:
    """Turn a live feed ``packetObservation`` into a remote RX log, or ``None`` to skip it.

    The feed carries no packet bytes, so instead of ``payload`` the RX log has ``decoded``:
    the header fields and path under the keys ``meshcore_packets.decode_packet`` uses. When
    MeshMapper knows who sent the packet, ``source_node`` names it. Our own uploads are skipped.
    """
    if not isinstance(data, dict):
        return None
    packet, observation = data.get("packet"), data.get("observation")
    if not isinstance(packet, dict) or not isinstance(observation, dict):
        return None
    observer_id = _clean_hex(observation.get("observerPublicKey"))
    if len(observer_id) != 64 or not HEX_RE.match(observer_id) or observer_id == own_public_key:
        return None
    hash_hex = _clean_hex(data.get("packetHash"))
    payload_type = _as_int(packet.get("payloadType"), -1)
    route_type = _as_int(packet.get("routeType"), -1)
    if len(hash_hex) != 16 or not HEX_RE.match(hash_hex) or not 0 <= payload_type <= 15 or route_type not in ROUTE_TYPE_NAMES:
        return None
    path_length = observation.get("pathLength")
    hash_size = _as_int(path_length.get("hashSize"), 1) if isinstance(path_length, dict) else 1
    path_hex = _clean_hex(observation.get("pathBytes"))
    width = 2 * hash_size
    if not 1 <= hash_size <= 4 or not HEX_RE.match(path_hex) or len(path_hex) % width:
        return None
    path = [path_hex[index : index + width] for index in range(0, len(path_hex), width)]

    decoded: dict[str, Any] = {
        "hash": hash_hex,
        "payload_type": payload_type,
        "payload_type_name": PAYLOAD_TYPE_NAMES.get(payload_type, f"TYPE_{payload_type}"),
        "route_type": route_type,
        "route_type_name": ROUTE_TYPE_NAMES[route_type],
        "route": "direct" if route_type in DIRECT_ROUTE_TYPES else "flood",
        "path": path,
        "path_hash_size": hash_size,
        "hops": len(path),
    }
    if payload_type == PAYLOAD_TYPE_TRACE:
        # As in decode_packet: a TRACE path holds per-hop SNRs, not repeater hashes.
        decoded["trace_snrs"] = [int.from_bytes(bytes.fromhex(value), "little", signed=True) / 4 for value in path]
        decoded["path"] = []

    result: dict[str, Any] = {
        "decoded": decoded,
        "observer_id": observer_id,
        "observer_name": str(observation.get("observerName") or "").strip() or None,
    }
    snr, rssi = _as_float(observation.get("snr")), _as_float(observation.get("rssi"))
    if not (snr == 0 and rssi == 0):  # 0/0: the observer didn't report signal
        for key, value in (("snr", snr), ("rssi", rssi)):
            if value is not None:
                result[key] = value

    source = observation.get("resolvedSource")
    nodes = source.get("nodes") if isinstance(source, dict) and source.get("confidence") == "high" else None
    if isinstance(nodes, list) and len(nodes) == 1 and isinstance(nodes[0], dict):
        source_key = _clean_hex(nodes[0].get("publicKey"))
        if len(source_key) == 64 and HEX_RE.match(source_key):
            source_node: dict[str, Any] = {
                "public_key": source_key,
                "name": str(nodes[0].get("name") or "").strip() or None,
            }
            lat, lon = nodes[0].get("latitude"), nodes[0].get("longitude")
            if is_valid_position(lat, lon):
                source_node["lat"], source_node["lon"] = float(lat), float(lon)
            result["source_node"] = source_node
    return result


# --- Auth token --------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_token_signing_input(public_key_hex: str, claims: dict[str, Any], now: int, ttl_seconds: int) -> str:
    header = {"alg": "Ed25519", "typ": "JWT"}
    payload = {"publicKey": public_key_hex.upper(), "iat": now, "exp": now + ttl_seconds}
    payload.update(claims)
    header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"{_b64url(header_json)}.{_b64url(payload_json)}"


def finish_token(signing_input: str, signature: bytes) -> str:
    return f"{signing_input}.{signature.hex().upper()}"


# --- Uploader ----------------------------------------------------------------


def _default_client_factory(client_id: str, transport: str) -> Any:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:  # pragma: no cover - guarded import
        raise RuntimeError("The meshmapper plugin requires paho-mqtt: pip install paho-mqtt") from exc

    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
        transport=transport,
    )


def _reason_code_value(reason_code: Any) -> int:
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _sanitize_client_id(prefix: str, public_key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", f"{prefix}{public_key}")[:23]


def _configure_client(client: Any, transport: str, tls: bool, tls_verify: bool, websocket_path: str) -> None:
    if tls:
        if tls_verify:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            client.tls_insecure_set(False)
        else:
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            LOGGER.warning("MeshMapper: TLS certificate verification is disabled")
    if transport == "websockets":
        client.ws_set_options(path=websocket_path)


def _is_failure(reason_code: Any) -> bool:
    return bool(getattr(reason_code, "is_failure", _reason_code_value(reason_code) != 0))


def _set_status(status: Optional[StatusRegistry], key: str, state: str, detail: str) -> None:
    if status is not None:
        status.set_state(key, state, detail, label=STATUS_LABELS[key])


class MeshMapperUploader:
    def __init__(
        self,
        config: MeshMapperConfig,
        client_factory: Callable[[str, str], Any] = _default_client_factory,
        status: Optional[StatusRegistry] = None,
    ):
        self.config = config
        self._client_factory = client_factory
        self.status = status
        self._transport: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None
        self._connected = False
        self._public_key = ""
        self._origin = "MeshCore Device"
        self._radio = "unknown"
        self._model = "unknown"
        self._firmware_version = "unknown"
        self._local_private_key: Optional[bytes] = None
        self._private_key_rejected = False
        self._token_task: Optional[asyncio.Task[None]] = None
        self._status_task: Optional[asyncio.Task[None]] = None
        self._refresh_now: Optional[asyncio.Event] = None

    # Identity ----------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def public_key(self) -> str:
        return self._public_key

    def _set_status(self, state: str, detail: str) -> None:
        _set_status(self.status, STATUS_PUBLISH, state, detail)

    def _topic(self, template: str) -> str:
        return template.replace("{IATA}", self.config.iata).replace("{PUBLIC_KEY}", self._public_key)

    async def on_device_connected(self, transport: Any) -> None:
        self._transport = transport
        self._loop = asyncio.get_running_loop()

        info = transport.device_self_info
        public_key = _clean_hex(info.get("public_key"))
        if len(public_key) != 64:
            LOGGER.error("MeshMapper: radio did not report a public key; uploads disabled until next connect")
            self._set_status(DISCONNECTED, "Radio did not report a public key")
            return

        if self._client is not None and public_key != self._public_key:
            LOGGER.warning("MeshMapper: radio identity changed; restarting MQTT session")
            await self.stop()

        self._public_key = public_key
        name = str(info.get("name") or "").strip()
        if name:
            self._origin = name
        radio_parts = [info.get(key) for key in ("radio_freq", "radio_bw", "radio_sf", "radio_cr")]
        if all(part is not None for part in radio_parts):
            self._radio = ",".join(str(part) for part in radio_parts)
        await self._refresh_device_info(transport)

        if self._token_task is None or self._token_task.done():
            self._refresh_now = asyncio.Event()
            self._token_task = asyncio.create_task(self._token_loop(), name="meshmapper-token")
            self._status_task = asyncio.create_task(self._status_loop(), name="meshmapper-status")
        else:
            self._publish_status("online")

    async def _refresh_device_info(self, transport: Any) -> None:
        try:
            device_info = await transport.query_device_info()
        except Exception as exc:
            LOGGER.debug("MeshMapper: device query failed: %s", exc)
            return
        model = str(device_info.get("model") or "").strip()
        version = str(device_info.get("ver") or "").strip()
        build = str(device_info.get("fw_build") or "").strip()
        if model:
            self._model = model
        if version:
            version = version if version.startswith("v") else f"v{version}"
            self._firmware_version = f"{version} (Build: {build})" if build else version
        elif device_info.get("fw ver") is not None:
            self._firmware_version = f"v{device_info.get('fw ver')}"

    # Token -----------------------------------------------------------------------

    def _private_key_bytes(self) -> Optional[bytes]:
        if not self.config.private_key or self._private_key_rejected:
            return None
        if self._local_private_key is None:
            key = bytes.fromhex(self.config.private_key)
            derived = public_key_from_expanded(key).hex().upper()
            if derived != self._public_key:
                LOGGER.error(
                    "MeshMapper: configured private_key does not belong to the connected radio "
                    "(derived %s…, radio %s…); falling back to on-device signing",
                    derived[:12],
                    self._public_key[:12],
                )
                self._private_key_rejected = True
                return None
            self._local_private_key = key
        return self._local_private_key

    async def create_token(self) -> str:
        claims: dict[str, Any] = {}
        if self.config.token_audience:
            claims["aud"] = self.config.token_audience
        claims["client"] = CLIENT_VERSION
        signing_input = build_token_signing_input(
            self._public_key, claims, int(time.time()), self.config.token_ttl_seconds
        )
        message = signing_input.encode("utf-8")

        private_key = self._private_key_bytes()
        if private_key is not None:
            signature = sign_with_expanded_key(message, private_key, bytes.fromhex(self._public_key))
        else:
            if self._transport is None or not self._transport.is_connected:
                raise RuntimeError("radio is not connected for on-device signing")
            signature = await self._transport.sign_with_device(message)
        return finish_token(signing_input, signature)

    async def _token_loop(self) -> None:
        refresh_seconds = max(60, self.config.token_ttl_seconds - 300)
        assert self._refresh_now is not None
        while True:
            try:
                token = await self.create_token()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("MeshMapper: could not create auth token (%s); retrying in 60s", exc)
                if not self._connected:
                    self._set_status(DISCONNECTED, f"Could not create auth token: {exc}")
                await asyncio.sleep(60)
                continue

            username = f"v1_{self._public_key}"
            if self._client is None:
                try:
                    self._start_client(username, token)
                except Exception as exc:
                    LOGGER.exception("MeshMapper: MQTT client setup failed; uploads disabled")
                    self._set_status(DISABLED, f"MQTT client setup failed: {exc}")
                    return
            else:
                self._client.username_pw_set(username, token)
                LOGGER.debug("MeshMapper: auth token refreshed")

            self._refresh_now.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._refresh_now.wait(), timeout=refresh_seconds)

    # MQTT --------------------------------------------------------------------------

    def _start_client(self, username: str, token: str) -> None:
        cfg = self.config
        client_id = _sanitize_client_id(cfg.client_id_prefix, self._public_key)
        client = self._client_factory(client_id, cfg.transport)
        client.username_pw_set(username, token)
        _configure_client(client, cfg.transport, cfg.tls, cfg.tls_verify, cfg.websocket_path)
        client.will_set(
            self._topic(cfg.topic_status),
            json.dumps(self.build_status("offline")),
            qos=0,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        client.connect_async(cfg.server, cfg.port, keepalive=cfg.keepalive)
        client.loop_start()
        self._client = client
        self._set_status(CONNECTING, f"Connecting to {cfg.server}:{cfg.port}")
        LOGGER.info(
            "MeshMapper: connecting to %s:%s (transport=%s, tls=%s, iata=%s, observer=%s)",
            cfg.server,
            cfg.port,
            cfg.transport,
            cfg.tls,
            cfg.iata,
            self._public_key,
        )

    # paho callbacks run on paho's network thread.
    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if _is_failure(reason_code):
            self._connected = False
            LOGGER.error("MeshMapper: MQTT connect refused: %s", reason_code)
            self._set_status(DISCONNECTED, f"Connect refused: {reason_code}")
            if _reason_code_value(reason_code) in MQTT_AUTH_FAILURE_CODES:
                self._request_token_refresh()
            return
        self._connected = True
        topic = self._topic(self.config.topic_packets)
        LOGGER.info("MeshMapper: connected; publishing packets to %s", topic)
        self._set_status(CONNECTED, f"{self.config.server} → {topic}")
        self._publish_status("online")

    def _on_connect_fail(self, client: Any, userdata: Any) -> None:
        # The broker couldn't be reached at all (DNS, network, TLS); paho keeps retrying.
        self._set_status(DISCONNECTED, f"Can't reach {self.config.server}:{self.config.port}; retrying")

    def _on_disconnect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        was_connected, self._connected = self._connected, False
        # A refused or failed connect has already reported a more useful reason.
        if self._client is None or not was_connected:
            return
        self._set_status(DISCONNECTED, f"Disconnected ({reason_code}); reconnecting")
        if _reason_code_value(reason_code) != 0:
            LOGGER.warning("MeshMapper: MQTT disconnected (%s); paho will reconnect", reason_code)
            if _reason_code_value(reason_code) in MQTT_AUTH_FAILURE_CODES:
                self._request_token_refresh()

    def _request_token_refresh(self) -> None:
        if self._loop is None or self._refresh_now is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._refresh_now.set)

    def build_status(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": self._origin,
            "origin_id": self._public_key,
            "model": self._model,
            "firmware_version": self._firmware_version,
            "radio": self._radio,
            "client_version": CLIENT_VERSION,
        }

    def _publish_status(self, status: str) -> Any:
        if self._client is None or not self._connected:
            return None
        try:
            return self._client.publish(
                self._topic(self.config.topic_status),
                json.dumps(self.build_status(status)),
                qos=0,
                retain=True,
            )
        except Exception as exc:
            LOGGER.warning("MeshMapper: status publish failed: %s", exc)
            return None

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.status_interval_seconds)
            self._publish_status("online")

    async def handle_rx_log(self, rx_log: dict[str, Any]) -> None:
        if self._client is None or not self._connected or not self._public_key:
            return
        message = build_packet_message(rx_log, self._origin, self._public_key)
        if message is None:
            LOGGER.debug("MeshMapper: skipping undecodable RX log: %s", rx_log.get("raw_hex"))
            return
        try:
            self._client.publish(self._topic(self.config.topic_packets), json.dumps(message), qos=0, retain=False)
        except Exception as exc:
            LOGGER.warning("MeshMapper: packet publish failed: %s", exc)
            return
        LOGGER.debug(
            "MeshMapper: published packet type=%s route=%s len=%s hash=%s",
            message["packet_type"],
            message["route"],
            message["len"],
            message["hash"],
        )

    async def stop(self) -> None:
        for task in (self._token_task, self._status_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._token_task = None
        self._status_task = None

        client = self._client
        if client is None:
            return
        info = self._publish_status("offline")
        self._client = None
        self._connected = False
        self._set_status(DISCONNECTED, "Stopped")
        if info is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(None, info.wait_for_publish, 2.0)
        with contextlib.suppress(Exception):
            client.disconnect()
        with contextlib.suppress(Exception):
            client.loop_stop()


# --- Subscriber ----------------------------------------------------------------


class MeshMapperSubscriber:
    """Read-only MQTT session that relays other observers' packets to the transport.

    It logs in with a subscriber account (username/password) because the broker
    only lets those subscribe; device-signed observer accounts are publish-only.
    """

    def __init__(
        self,
        config: MeshMapperConfig,
        client_factory: Callable[[str, str], Any] = _default_client_factory,
        status: Optional[StatusRegistry] = None,
    ):
        self.config = config
        self._client_factory = client_factory
        self.status = status
        self._transport: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None
        self._own_public_key = ""
        self._topic = ""
        self._connected = False
        self._subscribed = False
        self._denied = False
        self._remote_seen = False
        self.received = 0

    @property
    def is_subscribed(self) -> bool:
        return self._subscribed

    def _set_status(self, state: str, detail: str) -> None:
        _set_status(self.status, STATUS_SUBSCRIBE, state, detail)

    def start(self, transport: Any, own_public_key: str) -> None:
        """Start the session (once); later calls only update the transport and our own key."""
        self._transport = transport
        self._loop = asyncio.get_running_loop()
        self._own_public_key = own_public_key
        if self._client is not None:
            return
        cfg = self.config
        self._topic = cfg.topic_subscribe.replace("{IATA}", cfg.iata).replace("{PUBLIC_KEY}", "+")
        client_id = _sanitize_client_id(f"{cfg.client_id_prefix}sub_", own_public_key)
        try:
            client = self._client_factory(client_id, cfg.subscribe_transport)
            client.username_pw_set(cfg.subscribe_username, cfg.subscribe_password)
            _configure_client(client, cfg.subscribe_transport, cfg.subscribe_tls, cfg.tls_verify, cfg.websocket_path)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_connect_fail = self._on_connect_fail
            client.on_subscribe = self._on_subscribe
            client.on_message = self._on_message
            client.reconnect_delay_set(min_delay=1, max_delay=120)
            client.connect_async(cfg.subscribe_server, cfg.subscribe_port, keepalive=cfg.keepalive)
            client.loop_start()
        except Exception as exc:
            LOGGER.exception("MeshMapper: subscriber MQTT client setup failed")
            self._set_status(DISABLED, f"MQTT client setup failed: {exc}")
            return
        self._client = client
        self._set_status(CONNECTING, f"Connecting to {cfg.subscribe_server}:{cfg.subscribe_port}")
        LOGGER.info(
            "MeshMapper: connecting to %s:%s as %s to receive other observers' packets (%s)",
            cfg.subscribe_server,
            cfg.subscribe_port,
            cfg.subscribe_username,
            self._topic,
        )

    # paho callbacks run on paho's network thread.
    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if _is_failure(reason_code):
            self._connected = False
            if _reason_code_value(reason_code) in MQTT_AUTH_FAILURE_CODES:
                LOGGER.error(
                    "MeshMapper: subscriber login refused (%s); check MESHMAPPER_SUBSCRIBE_USERNAME/PASSWORD",
                    reason_code,
                )
                self._set_status(DISCONNECTED, f"Login refused ({reason_code}); check subscriber credentials")
            else:
                LOGGER.error("MeshMapper: subscriber connect refused: %s", reason_code)
                self._set_status(DISCONNECTED, f"Connect refused: {reason_code}")
            return
        self._connected = True
        if self._denied:
            return
        try:
            client.subscribe(self._topic, qos=0)
        except Exception as exc:
            LOGGER.warning("MeshMapper: could not subscribe to %s: %s", self._topic, exc)
            self._set_status(DISCONNECTED, f"Subscribe failed: {exc}")
            return
        self._set_status(CONNECTING, f"Connected; subscribing to {self._topic}")

    def _on_connect_fail(self, client: Any, userdata: Any) -> None:
        cfg = self.config
        self._set_status(DISCONNECTED, f"Can't reach {cfg.subscribe_server}:{cfg.subscribe_port}; retrying")

    def _on_disconnect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        was_connected, self._connected = self._connected, False
        self._subscribed = False
        if self._client is None or self._denied or not was_connected:
            return
        if _reason_code_value(reason_code) != 0:
            LOGGER.warning("MeshMapper: subscriber MQTT disconnected (%s); paho will reconnect", reason_code)
        self._set_status(DISCONNECTED, f"Disconnected ({reason_code}); reconnecting")

    def _on_subscribe(self, client: Any, userdata: Any, mid: Any, reason_codes: Any, properties: Any = None) -> None:
        codes = reason_codes if isinstance(reason_codes, (list, tuple)) else [reason_codes]
        failed = [code for code in codes if getattr(code, "is_failure", _reason_code_value(code) >= 0x80)]
        if failed:
            # Retrying won't help until the account's permissions change.
            self._denied = True
            LOGGER.warning(
                "MeshMapper: the broker refused the subscription to %s (%s); not retrying until restart",
                self._topic,
                failed[0],
            )
            self._set_status(DISABLED, f"Broker refused the subscription to {self._topic} ({failed[0]})")
            with contextlib.suppress(Exception):
                client.disconnect()
            return
        self._subscribed = True
        LOGGER.info("MeshMapper: subscribed to %s for other observers' packets", self._topic)
        self._set_status(CONNECTED, f"{self.config.subscribe_server} ← {self._topic}")

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        observation = parse_observer_packet(str(message.topic), message.payload, self._own_public_key)
        if observation is None:
            return
        dispatch = getattr(self._transport, "dispatch_remote_rx_log", None)
        if self._loop is None or self._loop.is_closed() or not callable(dispatch):
            return
        self.received += 1
        if not self._remote_seen:
            self._remote_seen = True
            LOGGER.info(
                "MeshMapper: receiving other observers' packets (first from %s)",
                observation.get("observer_name") or observation["observer_id"][:12],
            )
        asyncio.run_coroutine_threadsafe(dispatch(observation), self._loop)

    async def stop(self) -> None:
        client = self._client
        if client is None:
            return
        self._client = None
        self._connected = False
        self._subscribed = False
        with contextlib.suppress(Exception):
            client.disconnect()
        with contextlib.suppress(Exception):
            client.loop_stop()
        if not self._denied:
            self._set_status(DISCONNECTED, "Stopped")


# --- Live feed -----------------------------------------------------------------


async def _default_ws_connect(url: str) -> Any:
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover - guarded import
        raise RuntimeError("The MeshMapper live feed requires websockets: pip install websockets") from exc

    # Keep-alive is the feed's own ping message, as in MeshMapper's web client.
    return await connect(url, user_agent_header=CLIENT_VERSION, ping_interval=None, open_timeout=20)


def feed_backoff_seconds(attempt: int, jitter: float) -> float:
    """Reconnect delay: exponential from 1s to 30s, ±25% jitter (``jitter`` in [-1, 1])."""
    base = min(FEED_BACKOFF_BASE_SECONDS * 2 ** min(attempt, FEED_BACKOFF_CAP_ATTEMPT), FEED_BACKOFF_MAX_SECONDS)
    return max(0.1, base + base * 0.25 * jitter)


class MeshMapperLiveFeed:
    """Receives other observers' packets from MeshMapper's live feed.

    This is the public websocket (``wss://analyzer.meshmapper.net/ws``) behind the
    "Visualize Live" view of MeshMapper's region maps, so it needs no account. It is
    filtered to our region. Each observation names the observer and carries the
    packet's hash, type, route, path and signal, but not its bytes, so channel
    messages can't be decrypted from it. The client follows the rules MeshMapper's
    web client does: a ping every 30s, reconnect after 65s of silence, and
    exponential backoff with jitter, longest when the feed is shedding load.
    """

    def __init__(
        self,
        config: MeshMapperConfig,
        connect: Callable[[str], Awaitable[Any]] = _default_ws_connect,
        status: Optional[StatusRegistry] = None,
    ):
        self.config = config
        self._connect = connect
        self.status = status
        self._transport: Any = None
        self._own_public_key = ""
        self._paused: Callable[[], bool] = lambda: False
        self._task: Optional[asyncio.Task[None]] = None
        self._subscribed = False
        self._failures = 0
        self._remote_seen = False
        self.received = 0

    @property
    def is_subscribed(self) -> bool:
        return self._subscribed

    @property
    def host(self) -> str:
        return urlsplit(self.config.live_feed_url).hostname or self.config.live_feed_url

    def _set_status(self, state: str, detail: str) -> None:
        _set_status(self.status, STATUS_FEED, state, detail)

    def start(self, transport: Any, own_public_key: str, paused: Optional[Callable[[], bool]] = None) -> None:
        """Start the feed (once); later calls only update the transport and our own key.

        While ``paused()`` is true, observations are dropped (another source delivers them).
        """
        self._transport = transport
        self._own_public_key = own_public_key
        if paused is not None:
            self._paused = paused
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="meshmapper-live-feed")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        attempt = 0
        while True:
            self._set_status(CONNECTING, f"Connecting to {self.host}")
            opened_at: Optional[float] = None
            close_code: Optional[int] = None
            try:
                ws = await self._connect(self.config.live_feed_url)
                opened_at = loop.time()
                try:
                    await self._session(ws)
                finally:
                    close_code = getattr(ws, "close_code", None)
                    with contextlib.suppress(Exception):
                        await ws.close()
                reason = "Connection closed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc) or type(exc).__name__

            was_subscribed, self._subscribed = self._subscribed, False
            if opened_at is not None and loop.time() - opened_at >= FEED_STABLE_SECONDS:
                attempt = 0
            if close_code in FEED_SHED_LOAD_CLOSE_CODES:
                attempt = max(attempt, FEED_BACKOFF_CAP_ATTEMPT)
            delay = feed_backoff_seconds(attempt, random.uniform(-1.0, 1.0))
            attempt += 1
            self._failures += 1
            if was_subscribed or self._failures == 1:
                LOGGER.warning("MeshMapper: live feed %s (%s); reconnecting", "lost" if was_subscribed else "unavailable", reason)
            else:
                LOGGER.debug("MeshMapper: live feed still unavailable (%s); retrying in %.0fs", reason, delay)
            self._set_status(DISCONNECTED, f"{reason}; reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)

    async def _session(self, ws: Any) -> None:
        keepalive = asyncio.create_task(self._keepalive(ws), name="meshmapper-live-feed-ping")
        try:
            while True:
                try:
                    text = await asyncio.wait_for(ws.recv(), timeout=FEED_SILENCE_SECONDS)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"Nothing received for {FEED_SILENCE_SECONDS:.0f}s") from None
                await self._handle(ws, text)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keepalive

    @staticmethod
    async def _keepalive(ws: Any) -> None:
        sequence = 0
        while True:
            await asyncio.sleep(FEED_PING_SECONDS)
            sequence += 1
            await ws.send(json.dumps({"v": 1, "type": "ping", "id": f"ping-{sequence}"}))

    async def _handle(self, ws: Any, text: Any) -> None:
        try:
            message = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "hello":
            # Every connection starts with hello. configure sets every option to exactly the
            # values sent (an omitted one turns off), so send them all, then subscribe.
            # includeRepeats: also later hearings of a packet by the same observer (other paths).
            await ws.send(json.dumps({
                "v": 1, "type": "configure", "id": "configure",
                "resolvePath": False, "includeObserverKey": True, "includeRepeats": True,
            }))
            await ws.send(json.dumps({
                "v": 1, "type": "subscribe", "id": "subscribe",
                "scope": {"events": ["packetObservation"], "iatas": [self.config.iata]},
            }))
        elif kind == "subscribed":
            self._subscribed = True
            self._failures = 0
            LOGGER.info("MeshMapper: receiving region %s's packets from the live feed (%s)", self.config.iata, self.host)
            self._set_status(CONNECTED, f"{self.host} ← region {self.config.iata}")
        elif kind == "event" and message.get("event") == "packetObservation":
            await self._on_observation(message.get("data"))
        elif kind == "error":
            LOGGER.warning("MeshMapper: live feed error %s: %s", message.get("code"), message.get("message"))

    async def _on_observation(self, data: Any) -> None:
        if self._paused():
            return
        observation = parse_feed_observation(data, self._own_public_key)
        dispatch = getattr(self._transport, "dispatch_remote_rx_log", None)
        if observation is None or not callable(dispatch):
            return
        self.received += 1
        if not self._remote_seen:
            self._remote_seen = True
            LOGGER.info(
                "MeshMapper: receiving other observers' packets (first from %s)",
                observation.get("observer_name") or observation["observer_id"][:12],
            )
        await dispatch(observation)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._subscribed = False
        self._set_status(DISCONNECTED, "Stopped")


# --- Plugin ------------------------------------------------------------------


class MeshMapperPlugin(BasePlugin):
    name = "meshmapper"

    def __init__(self, settings: Optional[dict[str, Any]] = None):
        super().__init__(settings)
        self.config = MeshMapperConfig.from_settings(self.settings)
        self.uploader = MeshMapperUploader(self.config)
        self.subscriber = MeshMapperSubscriber(self.config)
        self.live_feed = MeshMapperLiveFeed(self.config)
        self._disabled_reason: Optional[str] = None
        self._subscriber_hint_logged = False

    def _bind_status(self, context: PluginContext) -> None:
        self.uploader.status = context.status
        self.subscriber.status = context.status
        self.live_feed.status = context.status

    def _disable(self, reason: str, context: PluginContext) -> None:
        if self._disabled_reason is None:
            LOGGER.error("MeshMapper uploads disabled: %s", reason)
            self._disabled_reason = reason
        for key in (STATUS_PUBLISH, STATUS_SUBSCRIBE, STATUS_FEED):
            _set_status(context.status, key, DISABLED, reason)

    def _startup_error(self, context: PluginContext) -> Optional[str]:
        if context.settings.mesh.backend != MESHCORE_BACKEND:
            return "MeshMapper only accepts MeshCore packets (set mesh.backend to meshcore)"
        return self.config.validation_error()

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        self._bind_status(context)
        error = self._startup_error(context)
        if error is not None:
            self._disable(error, context)
            return []
        _set_status(context.status, STATUS_PUBLISH, CONNECTING, "Waiting for the radio")
        for key, off in (
            (STATUS_SUBSCRIBE, self.config.subscriber_disabled_reason()),
            (STATUS_FEED, self.config.live_feed_disabled_reason()),
        ):
            if off is None:
                _set_status(context.status, key, CONNECTING, "Waiting for the radio")
            else:
                _set_status(context.status, key, DISABLED, off)
        return []

    async def on_mesh_connected(self, transport: Any, context: PluginContext) -> None:
        self._bind_status(context)
        error = self._startup_error(context)
        if error is not None:
            self._disable(error, context)
            return

        transport.add_rx_log_listener(self.uploader.handle_rx_log)
        await self.uploader.on_device_connected(transport)
        if not self.uploader.public_key:
            return

        feed_off = self.config.live_feed_disabled_reason()
        if feed_off is None:
            # The MQTT subscription, when there is one, carries the full packets: prefer it.
            self.live_feed.start(transport, self.uploader.public_key, paused=lambda: self.subscriber.is_subscribed)
        subscriber_off = self.config.subscriber_disabled_reason()
        if subscriber_off is None:
            self.subscriber.start(transport, self.uploader.public_key)
        elif feed_off is not None and self.config.subscribe and not self._subscriber_hint_logged:
            self._subscriber_hint_logged = True
            LOGGER.info(
                "MeshMapper: not receiving other observers' packets (live feed: %s; MQTT: %s). Uploads are unaffected.",
                feed_off,
                subscriber_off,
            )

    async def on_shutdown(self) -> None:
        await self.live_feed.stop()
        await self.subscriber.stop()
        await self.uploader.stop()

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

The plugin never emits bridge actions and only listens to raw RF logs, so it
does not interact with the other plugins.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from meshgram import __version__
from meshgram._ed25519 import public_key_from_expanded, sign_with_expanded_key
from meshgram.config import MESHCORE_BACKEND
from meshgram.plugin import BasePlugin
from meshgram.types import PluginContext

LOGGER = logging.getLogger(__name__)

DEFAULT_SERVER = "mqtt.meshmapper.net"
DEFAULT_TOPIC_STATUS = "meshcore/{IATA}/{PUBLIC_KEY}/status"
DEFAULT_TOPIC_PACKETS = "meshcore/{IATA}/{PUBLIC_KEY}/packets"
PLACEHOLDER_IATA_CODES = {"", "XXX", "XYZ"}
CLIENT_VERSION = f"meshgram/{__version__}"

PAYLOAD_TYPE_TRACE = 9
ROUTE_TYPE_TRANSPORT_FLOOD = 0x00
ROUTE_TYPE_TRANSPORT_DIRECT = 0x03
DIRECT_ROUTE_TYPES = {0x02, ROUTE_TYPE_TRANSPORT_DIRECT}
MQTT_AUTH_FAILURE_CODES = {4, 5, 134, 135}

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
    private_key: str = ""

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "MeshMapperConfig":
        iata = os.getenv("MESHMAPPER_IATA") or settings.get("iata") or ""
        private_key = os.getenv("MESHMAPPER_PRIVATE_KEY") or settings.get("private_key") or ""
        server = str(settings.get("server") or DEFAULT_SERVER).strip()
        transport = str(settings.get("transport") or "websockets").strip().lower()
        return cls(
            iata=str(iata).strip().upper(),
            server=server,
            port=_as_int(settings.get("port"), 443),
            transport=transport if transport in {"websockets", "tcp"} else "websockets",
            websocket_path=str(settings.get("websocket_path") or "/"),
            tls=_as_bool(settings.get("tls"), True),
            tls_verify=_as_bool(settings.get("tls_verify"), True),
            keepalive=max(10, _as_int(settings.get("keepalive"), 60)),
            token_audience=str(settings.get("token_audience", server) or "").strip(),
            token_ttl_seconds=max(600, _as_int(settings.get("token_ttl_seconds"), 3600)),
            status_interval_seconds=max(30, _as_int(settings.get("status_interval_seconds"), 300)),
            client_id_prefix=str(settings.get("client_id_prefix") or "meshgram_"),
            topic_status=str(settings.get("topic_status") or DEFAULT_TOPIC_STATUS),
            topic_packets=str(settings.get("topic_packets") or DEFAULT_TOPIC_PACKETS),
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


# --- Packet formatting -------------------------------------------------------


def parse_packet(raw: bytes) -> Optional[dict[str, Any]]:
    """Decode the MeshCore packet header. Returns ``None`` for truncated packets."""
    if len(raw) < 2:
        return None
    header = raw[0]
    route_type = header & 0x03
    payload_type = (header >> 2) & 0x0F
    offset = 1
    if route_type in (ROUTE_TYPE_TRANSPORT_FLOOD, ROUTE_TYPE_TRANSPORT_DIRECT):
        offset += 4  # two 16-bit transport codes
    if len(raw) <= offset:
        return None
    path_len_byte = raw[offset]
    offset += 1
    hash_size = (path_len_byte >> 6) + 1
    hop_count = path_len_byte & 0x3F
    path_end = offset + hop_count * hash_size
    if path_end > len(raw):
        return None
    path = raw[offset:path_end]
    return {
        "route_type": route_type,
        "payload_type": payload_type,
        "path_len_byte": path_len_byte,
        "path_hashes": [path[i : i + hash_size].hex().upper() for i in range(0, len(path), hash_size)],
        "payload": raw[path_end:],
    }


def packet_hash(payload_type: int, path_len_byte: int, payload: bytes) -> str:
    """Same as MeshCore ``Packet::calculatePacketHash`` (first 8 bytes of SHA-256)."""
    digest = hashlib.sha256()
    digest.update(bytes([payload_type]))
    if payload_type == PAYLOAD_TYPE_TRACE:
        digest.update(path_len_byte.to_bytes(2, "little"))
    digest.update(payload)
    return digest.hexdigest()[:16].upper()


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


class MeshMapperUploader:
    def __init__(
        self,
        config: MeshMapperConfig,
        client_factory: Callable[[str, str], Any] = _default_client_factory,
    ):
        self.config = config
        self._client_factory = client_factory
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

    def _topic(self, template: str) -> str:
        return template.replace("{IATA}", self.config.iata).replace("{PUBLIC_KEY}", self._public_key)

    async def on_device_connected(self, transport: Any) -> None:
        self._transport = transport
        self._loop = asyncio.get_running_loop()

        info = transport.device_self_info
        public_key = _clean_hex(info.get("public_key"))
        if len(public_key) != 64:
            LOGGER.error("MeshMapper: radio did not report a public key; uploads disabled until next connect")
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
                await asyncio.sleep(60)
                continue

            username = f"v1_{self._public_key}"
            if self._client is None:
                try:
                    self._start_client(username, token)
                except Exception:
                    LOGGER.exception("MeshMapper: MQTT client setup failed; uploads disabled")
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
        if cfg.tls:
            if cfg.tls_verify:
                client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                client.tls_insecure_set(False)
            else:
                client.tls_set(cert_reqs=ssl.CERT_NONE)
                client.tls_insecure_set(True)
                LOGGER.warning("MeshMapper: TLS certificate verification is disabled")
        if cfg.transport == "websockets":
            client.ws_set_options(path=cfg.websocket_path)
        client.will_set(
            self._topic(cfg.topic_status),
            json.dumps(self.build_status("offline")),
            qos=0,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        client.connect_async(cfg.server, cfg.port, keepalive=cfg.keepalive)
        client.loop_start()
        self._client = client
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
        if getattr(reason_code, "is_failure", _reason_code_value(reason_code) != 0):
            self._connected = False
            LOGGER.error("MeshMapper: MQTT connect refused: %s", reason_code)
            if _reason_code_value(reason_code) in MQTT_AUTH_FAILURE_CODES:
                self._request_token_refresh()
            return
        self._connected = True
        LOGGER.info("MeshMapper: connected; publishing packets to %s", self._topic(self.config.topic_packets))
        self._publish_status("online")

    def _on_disconnect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        self._connected = False
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
        if info is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(None, info.wait_for_publish, 2.0)
        with contextlib.suppress(Exception):
            client.disconnect()
        with contextlib.suppress(Exception):
            client.loop_stop()


# --- Plugin ------------------------------------------------------------------


class MeshMapperPlugin(BasePlugin):
    name = "meshmapper"

    def __init__(self, settings: Optional[dict[str, Any]] = None):
        super().__init__(settings)
        self.config = MeshMapperConfig.from_settings(self.settings)
        self.uploader = MeshMapperUploader(self.config)
        self._disabled_reason_logged = False

    def _disable(self, reason: str) -> None:
        if not self._disabled_reason_logged:
            LOGGER.error("MeshMapper uploads disabled: %s", reason)
            self._disabled_reason_logged = True

    async def on_mesh_connected(self, transport: Any, context: PluginContext) -> None:
        if context.settings.mesh.backend != MESHCORE_BACKEND:
            self._disable("MeshMapper only accepts MeshCore packets (set mesh.backend to meshcore)")
            return
        error = self.config.validation_error()
        if error is not None:
            self._disable(error)
            return

        transport.add_rx_log_listener(self.uploader.handle_rx_log)
        await self.uploader.on_device_connected(transport)

    async def on_shutdown(self) -> None:
        await self.uploader.stop()

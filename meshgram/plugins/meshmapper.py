"""MeshMapper (https://meshmapper.net) observer: uploads the packets the radio hears over MQTT.

This turns the MeshCore companion radio connected to Meshgram into a MeshMapper
*observer*, with the wire protocol and device-signed login every MeshCore MQTT
observer uses (see ``mqtt_observer``).

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
listeners (the packet_map plugin shows them; channel messages reach every plugin).

The plugin never emits bridge actions itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from meshgram.meshcore_packets import (
    DIRECT_ROUTE_TYPES,
    PAYLOAD_TYPE_NAMES,
    PAYLOAD_TYPE_TRACE,
    ROUTE_TYPE_NAMES,
    is_valid_position,
    packet_hash,
)
from meshgram.plugin import BasePlugin
from meshgram.status import CONNECTED, CONNECTING, DISABLED, DISCONNECTED, StatusRegistry
from meshgram.types import PluginAction, PluginContext

from .mqtt_observer import (
    CLIENT_VERSION,
    HEX_RE,
    OWNER_SETTINGS,
    ObserverConfig,
    ObserverSubscriber,
    ObserverUploader,
    _as_bool,
    _as_float,
    _as_int,
    _clean_hex,
    build_packet_message,
    build_token_signing_input,
    finish_token,
    parse_observer_packet,
    set_status,
)

__all__ = [
    "CLIENT_VERSION",
    "MeshMapperConfig",
    "MeshMapperLiveFeed",
    "MeshMapperPlugin",
    "MeshMapperSubscriber",
    "MeshMapperUploader",
    "build_packet_message",
    "build_token_signing_input",
    "feed_backoff_seconds",
    "finish_token",
    "packet_hash",
    "parse_feed_observation",
    "parse_observer_packet",
]

LOGGER = logging.getLogger(__name__)

NETWORK = "MeshMapper"
DEFAULT_SERVER = "mqtt.meshmapper.net"
DEFAULT_LIVE_FEED_URL = "wss://analyzer.meshmapper.net/ws"

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

PRESET: dict[str, Any] = {
    "label": NETWORK,
    "network": NETWORK,
    "plugin": "meshmapper",
    "server": DEFAULT_SERVER,
    "port": 443,
    "transport": "websockets",
    "tls": True,
    "auth": "token",
    "status_publish": STATUS_PUBLISH,
    "status_subscribe": STATUS_SUBSCRIBE,
}

# The observer sessions are MeshCore's standard ones.
MeshMapperUploader = ObserverUploader
MeshMapperSubscriber = ObserverSubscriber


# --- Settings ----------------------------------------------------------------


@dataclass(slots=True)
class MeshMapperConfig(ObserverConfig):
    # Public, account-free feed of every observer's packets (metadata only).
    live_feed: bool = True
    live_feed_url: str = DEFAULT_LIVE_FEED_URL

    @classmethod
    def from_settings(cls, settings: dict[str, Any], preset: Optional[dict[str, Any]] = None) -> "MeshMapperConfig":
        values = cls.values_from_settings(settings, {**PRESET, **(preset or {})})
        values["live_feed"] = _as_bool(settings.get("live_feed"), True)
        values["live_feed_url"] = str(settings.get("live_feed_url") or DEFAULT_LIVE_FEED_URL).strip()
        return cls(**values)

    def live_feed_disabled_reason(self) -> Optional[str]:
        """Why the live feed won't run, or ``None`` if it should."""
        if not self.subscribe:
            return "Turned off (subscribe: false)"
        if not self.live_feed or not self.live_feed_url:
            return "Turned off (live_feed: false)"
        return None


def _set_status(status: Optional[StatusRegistry], key: str, state: str, detail: str) -> None:
    set_status(status, key, state, detail, STATUS_LABELS[key])


# --- Live feed observations ----------------------------------------------------------


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
        "network": NETWORK,
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
    title = "MeshMapper observer"
    description = (
        "Uploads every packet the radio hears to MeshMapper (meshmapper.net) as an observer, "
        "and receives other observers' packets for the map."
    )
    settings_schema = {
        "type": "object",
        "required": ["iata"],
        "properties": {
            "iata": {
                "type": "string",
                "title": "Region code",
                "description": "Your MeshMapper region (an airport code, like YUL).",
                "pattern": "^[A-Za-z0-9]{2,8}$",
                "x-pattern-message": "must be a region code like YUL",
            },
            **OWNER_SETTINGS,
            "subscribe": {
                "type": "boolean",
                "title": "Receive other observers' packets",
                "default": True,
            },
            "live_feed": {
                "type": "boolean",
                "title": "Use MeshMapper's public live feed",
                "description": "No account needed, but packets come without their bytes (no decryption).",
                "default": True,
            },
            "subscribe_username": {
                "type": "string",
                "title": "Subscriber account",
                "description": "From the broker operator: receives full packets over MQTT.",
            },
            "subscribe_password": {"type": "string", "writeOnly": True, "title": "Subscriber password"},
            "server": {"type": "string", "minLength": 1, "title": "MQTT server", "default": DEFAULT_SERVER},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "title": "MQTT port", "default": 443},
            "transport": {"type": "string", "enum": ["websockets", "tcp"], "title": "MQTT transport", "default": "websockets"},
            "token_audience": {"type": "string", "title": "Token audience", "default": DEFAULT_SERVER},
            "status_interval_seconds": {
                "type": "integer",
                "minimum": 30,
                "title": "Status update interval (seconds)",
                "default": 300,
            },
            "subscribe_server": {"type": "string", "title": "Subscriber MQTT server", "description": "Empty: the MQTT server."},
            "subscribe_port": {"type": "integer", "minimum": 1, "maximum": 65535, "title": "Subscriber MQTT port"},
            "private_key": {
                "type": "string",
                "writeOnly": True,
                "title": "Radio private key",
                "description": "Only if the firmware can't sign on the radio itself (128 hex characters).",
                "pattern": "^[0-9a-fA-F]{128}$",
                "x-pattern-message": "must be 128 hex characters",
            },
        },
    }

    def __init__(self, settings: Optional[dict[str, Any]] = None):
        super().__init__(settings)
        self.config = MeshMapperConfig.from_settings(self.settings)
        self.uploader = MeshMapperUploader(self.config)
        self.subscriber = MeshMapperSubscriber(self.config)
        self.live_feed = MeshMapperLiveFeed(self.config)
        self._disabled_reason: Optional[str] = None
        self._subscriber_hint_logged = False
        self._transport: Any = None
        self._status: Optional[StatusRegistry] = None

    def _bind_status(self, context: PluginContext) -> None:
        self._status = context.status
        self.uploader.status = context.status
        self.subscriber.status = context.status
        self.live_feed.status = context.status

    def _disable(self, reason: str, context: PluginContext) -> None:
        if self._disabled_reason is None:
            LOGGER.error("MeshMapper uploads disabled: %s", reason)
            self._disabled_reason = reason
        for key in (STATUS_PUBLISH, STATUS_SUBSCRIBE, STATUS_FEED):
            _set_status(context.status, key, DISABLED, reason)

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        self._bind_status(context)
        error = self.config.validation_error()
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
        error = self.config.validation_error()
        if error is not None:
            self._disable(error, context)
            return

        self._transport = transport
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
        if self._transport is not None:
            self._transport.remove_rx_log_listener(self.uploader.handle_rx_log)
            self._transport = None
        await self.live_feed.stop()
        await self.subscriber.stop()
        await self.uploader.stop()
        # Turned off: its connections no longer apply.
        if self._status is not None:
            for key in (STATUS_PUBLISH, STATUS_SUBSCRIBE, STATUS_FEED):
                self._status.remove(key)

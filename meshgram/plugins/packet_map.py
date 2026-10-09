"""Live web map of MeshCore packet propagation.

Serves a small web app (stdlib asyncio HTTP server, no extra dependencies) with
a map of every node that shares a position (adverts and radio contacts,
repeaters highlighted) next to a live list of every RF packet the radio hears.
Repeater hashes in each packet's path are resolved to known nodes so the route
a packet took can be drawn on the map.

Packets heard by other MeshMapper observers (relayed by the meshmapper plugin
through the transport's remote RX log listeners) are shown too, routed to the
observer that heard them, in a buffer of their own.

Nodes and packet history are saved to SQLite (see ``packet_map_store``) and
restored on startup, so the map survives restarts and redeploys.

The plugin never emits bridge actions; it only listens to raw RF logs.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from meshgram.config import MESHCORE_BACKEND, _as_bool
from meshgram.meshcore_packets import (
    NODE_TYPE_NAMES,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_ANON_REQ,
    PAYLOAD_TYPE_GRP_TXT,
    decode_packet,
    decrypt_group_text,
    is_valid_position,
    parse_packet,
)
from meshgram.plugin import BasePlugin
from meshgram.plugins.packet_map_store import (
    PacketMapStore,
    PacketMapStoreError,
    PendingChanges,
    RetentionLimits,
    SavedState,
    encode,
)
from meshgram.status import StatusRegistry
from meshgram.types import PluginAction, PluginContext

LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("packet_map_static")
DEFAULT_TITLE = "Meshgram"
HEX_RE = re.compile(r"^[0-9a-fA-F]*$")
# Node types that relay packets and therefore appear in packet paths.
RELAY_NODE_TYPES = {"repeater", "room"}
SSE_KEEPALIVE_SECONDS = 15.0
SSE_QUEUE_SIZE = 500
CONTACT_REFRESH_SECONDS = 30.0
MAX_REQUEST_HEAD_BYTES = 16 * 1024
PERSIST_INTERVAL_SECONDS = 5.0
DEFAULT_DATA_DIR = "data"
DEFAULT_DB_FILE = "packet_map.sqlite3"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class PacketMapConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    max_packets: int = 500
    max_messages: int = 1000
    max_remote_packets: int = 1000
    password: str = ""
    title: str = DEFAULT_TITLE
    # Empty: the page restyles OpenStreetMap tiles to match its light or dark theme.
    tile_url: str = ""
    tile_attribution: str = ""
    # Where nodes and packet history are kept across restarts; None keeps them in memory only.
    db_path: Optional[Path] = None

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "PacketMapConfig":
        host = os.getenv("PACKET_MAP_HOST") or settings.get("host") or "127.0.0.1"
        port = os.getenv("PACKET_MAP_PORT") or settings.get("port")
        password = os.getenv("PACKET_MAP_PASSWORD") or settings.get("password") or ""
        db_path = None
        if _as_bool(settings.get("persist"), True):
            # A relative path is relative to MESHGRAM_DATA_DIR (an absolute one is used as-is).
            data_dir = Path(os.getenv("MESHGRAM_DATA_DIR") or DEFAULT_DATA_DIR)
            db_path = data_dir / str(os.getenv("PACKET_MAP_DB_PATH") or settings.get("db_path") or DEFAULT_DB_FILE)
        return cls(
            host=str(host).strip(),
            port=_as_int(port, 8080),
            max_packets=max(10, _as_int(settings.get("max_packets"), 500)),
            max_messages=max(10, _as_int(settings.get("max_messages"), 1000)),
            max_remote_packets=max(10, _as_int(settings.get("max_remote_packets"), 1000)),
            password=str(password),
            title=str(settings.get("title") or DEFAULT_TITLE),
            tile_url=str(settings.get("tile_url") or ""),
            tile_attribution=str(settings.get("tile_attribution") or ""),
            db_path=db_path,
        )

    def client_config(self) -> dict[str, Any]:
        return {"title": self.title, "tile_url": self.tile_url, "tile_attribution": self.tile_attribution}


# --- State -------------------------------------------------------------------


def _clean_key(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    text = "".join(str(value or "").split()).upper()
    return text if HEX_RE.match(text) else ""


def _node_type_name(value: Any) -> str:
    if isinstance(value, str) and value in NODE_TYPE_NAMES.values():
        return value
    return NODE_TYPE_NAMES.get(_as_int(value, 0), "unknown")


def _distance_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


def _has_position(node: Optional[dict[str, Any]]) -> bool:
    return node is not None and node.get("lat") is not None and node.get("lon") is not None


class PacketMapState:
    """Node registry and packet ring buffer backing the web app."""

    def __init__(self, max_packets: int = 500, max_messages: int = 1000, max_remote_packets: int = 1000):
        self.nodes: dict[str, dict[str, Any]] = {}
        self.packets: deque[dict[str, Any]] = deque(maxlen=max_packets)
        # Packets other MeshMapper observers heard; kept apart so a busy region
        # can't push this radio's own packets out of the buffer.
        self.remote_packets: deque[dict[str, Any]] = deque(maxlen=max_remote_packets)
        # Channel secrets (``name``, ``secret``, ``hash``) for decrypting channel
        # messages that meshcore_py didn't decrypt, such as other observers' packets.
        self.channels: list[dict[str, Any]] = []
        # Decrypted messages are kept longer than the packet buffer, which busy
        # meshes fill with adverts and ACKs within minutes.
        self.messages: deque[dict[str, Any]] = deque(maxlen=max_messages)
        self.self_id: Optional[str] = None
        # Connection status of the radio, Telegram, MQTT, ... (shown in the page header).
        self.status: Optional[StatusRegistry] = None
        self._hash_counts: dict[str, int] = {}
        self._remote_hash_counts: dict[str, int] = {}
        self._next_packet_id = 1
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # Changes not yet persisted (an ordered set, so nodes keep their order on
        # disk); None until track_changes(), i.e. when persistence is off.
        self._dirty_node_ids: Optional[dict[str, None]] = None
        self._unsaved_packets: list[dict[str, Any]] = []

    # Persistence ------------------------------------------------------------

    def restore(self, saved: SavedState) -> None:
        """Load nodes and packets (oldest first) saved by a previous run."""
        for node in saved.nodes:
            if isinstance(node, dict) and node.get("id"):
                self.nodes[node["id"]] = node
        self.self_id = saved.self_id
        for packet in saved.packets:
            # Keep the "heard N×" count the packet had when it arrived.
            seen_count = packet.get("seen_count")
            if packet.get("source") == "meshmapper":
                self._track(self.remote_packets, self._remote_hash_counts, packet)
            else:
                self._track(self.packets, self._hash_counts, packet)
            if seen_count:
                packet["seen_count"] = seen_count
            if packet.get("message"):
                self.messages.append(packet)
            self._next_packet_id = max(self._next_packet_id, _as_int(packet.get("id"), 0) + 1)

    def track_changes(self) -> None:
        if self._dirty_node_ids is None:
            self._dirty_node_ids = {}

    def drain_changes(self) -> Optional[PendingChanges]:
        """Serialize and clear what changed since the last call (None if nothing did)."""
        if not self._dirty_node_ids and not self._unsaved_packets:
            return None
        node_ids = self._dirty_node_ids or {}
        nodes = [(node_id, encode(self.nodes[node_id])) for node_id in node_ids if node_id in self.nodes]
        packets = [
            (
                packet["id"],
                packet["received_at"],
                int(packet.get("source") == "meshmapper"),
                int(bool(packet.get("message"))),
                encode(packet),
            )
            for packet in self._unsaved_packets
        ]
        self._dirty_node_ids = {}
        self._unsaved_packets = []
        return PendingChanges(self_id=self.self_id, nodes=nodes, packets=packets)

    def _mark_dirty(self, node_id: str) -> None:
        if self._dirty_node_ids is not None:
            self._dirty_node_ids[node_id] = None

    # Subscribers ------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client that can't keep up gets disconnected and re-syncs on reconnect.
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait({"type": "overflow"})

    def apply_status(self, entry: dict[str, Any]) -> None:
        self._publish({"type": "connection", "connection": entry})

    # Nodes ------------------------------------------------------------------

    def _upsert_node(self, node_id: str, **fields: Any) -> tuple[dict[str, Any], bool]:
        node = self.nodes.get(node_id)
        created = node is None
        if node is None:
            node = {"id": node_id, "name": None, "type": "unknown", "lat": None, "lon": None}
            self.nodes[node_id] = node
        before = dict(node)
        for key, value in fields.items():
            if value is None:
                continue
            if key == "type" and value == "unknown" and node.get("type") != "unknown":
                continue
            node[key] = value
        changed = created or node != before
        if changed:
            self._mark_dirty(node_id)
        return node, changed

    def update_self(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        node_id = _clean_key(info.get("public_key"))
        if not node_id:
            return []
        if self.self_id and self.self_id != node_id and self.self_id in self.nodes:
            self.nodes[self.self_id]["is_self"] = False
            self._mark_dirty(self.self_id)
        self.self_id = node_id
        lat, lon = info.get("adv_lat"), info.get("adv_lon")
        position = {"lat": float(lat), "lon": float(lon)} if is_valid_position(lat, lon) else {}
        node, changed = self._upsert_node(
            node_id,
            name=str(info.get("name") or "").strip() or None,
            type=_node_type_name(info.get("adv_type", info.get("type"))),
            is_self=True,
            **position,
        )
        return [dict(node)] if changed else []

    def update_contacts(self, contacts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        changed_nodes = []
        for key, contact in contacts.items():
            node_id = _clean_key(contact.get("public_key") or key)
            if len(node_id) != 64:
                continue
            lat, lon = contact.get("adv_lat"), contact.get("adv_lon")
            position = {"lat": float(lat), "lon": float(lon)} if is_valid_position(lat, lon) else {}
            last_advert = _as_int(contact.get("last_advert"), 0)
            known_advert = (self.nodes.get(node_id) or {}).get("last_advert") or 0
            # An advert we heard ourselves may be newer than the radio's contact entry.
            newer = last_advert >= known_advert
            node, changed = self._upsert_node(
                node_id,
                name=(str(contact.get("adv_name") or "").strip() or None) if newer else None,
                type=_node_type_name(contact.get("type")),
                last_advert=(last_advert or None) if newer else None,
                is_contact=True,
                **(position if newer else {}),
            )
            if changed:
                changed_nodes.append(dict(node))
        return changed_nodes

    def apply_contacts(self, contacts: dict[str, dict[str, Any]]) -> None:
        changed = self.update_contacts(contacts)
        if changed:
            self._publish({"type": "nodes", "nodes": changed})

    def apply_self(self, info: dict[str, Any]) -> None:
        changed = self.update_self(info)
        self._publish({"type": "self", "self_id": self.self_id, "nodes": changed})

    def _candidates(self, hash_hex: str) -> list[dict[str, Any]]:
        return [node for node_id, node in self.nodes.items() if node_id.startswith(hash_hex)]

    def _pick(self, candidates: list[dict[str, Any]], anchor: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not candidates:
            return None
        relays = [node for node in candidates if node.get("type") in RELAY_NODE_TYPES]
        pool = relays or candidates
        if len(pool) == 1:
            return pool[0]
        placed = [node for node in pool if _has_position(node)]
        if _has_position(anchor) and placed:
            # Radio range is limited: the nearest candidate is the likeliest hop.
            return min(placed, key=lambda node: _distance_km(node, anchor))  # type: ignore[arg-type]
        return placed[0] if len(placed) == 1 else None

    def _node_ref(self, node: Optional[dict[str, Any]], hash_hex: str, candidates: int) -> dict[str, Any]:
        return {
            "hash": hash_hex,
            "node_id": node["id"] if node else None,
            "name": node.get("name") if node else None,
            "candidates": candidates,
        }

    def _resolve_path(self, path: list[str], receiver_id: Optional[str] = None) -> list[dict[str, Any]]:
        # Walk backwards from the radio that heard the packet (ours unless another
        # observer's position is known): each hop is picked relative to the next one.
        anchor = self.nodes.get(receiver_id) if receiver_id else None
        if not _has_position(anchor):
            anchor = self.nodes.get(self.self_id) if self.self_id else None
        resolved: list[dict[str, Any]] = []
        for hash_hex in reversed(path):
            candidates = self._candidates(hash_hex)
            node = self._pick(candidates, anchor)
            resolved.append(self._node_ref(node, hash_hex, len(candidates)))
            if _has_position(node):
                anchor = node
        resolved.reverse()
        return resolved

    def _resolve_origin(self, packet: dict[str, Any]) -> Optional[dict[str, Any]]:
        key = (packet.get("advert") or {}).get("public_key") or packet.get("src_public_key")
        if key and key in self.nodes:
            node = self.nodes[key]
            return self._node_ref(node, key[:2], 1)
        if packet.get("src_hash"):
            candidates = self._candidates(packet["src_hash"])
            node = candidates[0] if len(candidates) == 1 else None
            return self._node_ref(node, packet["src_hash"], len(candidates))
        sender = packet.get("sender_name")
        if sender:
            matches = [node for node in self.nodes.values() if node.get("name") == sender]
            if len(matches) == 1:
                return self._node_ref(matches[0], matches[0]["id"][:2], 1)
        return None

    # Packets ----------------------------------------------------------------

    def ingest_rx_log(self, rx_log: dict[str, Any], now: Optional[float] = None) -> Optional[dict[str, Any]]:
        return self._ingest(rx_log, now, observer_id=None)

    def ingest_remote_rx_log(self, rx_log: dict[str, Any], now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Ingest a packet another MeshMapper observer heard (``observer_id``/``observer_name`` keys)."""
        observer_id = _clean_key(rx_log.get("observer_id"))
        if not observer_id or observer_id == self.self_id:
            return None
        return self._ingest(rx_log, now, observer_id=observer_id)

    def _decrypt_channel_message(self, raw: bytes) -> Optional[dict[str, Any]]:
        if not self.channels:
            return None
        parsed = parse_packet(raw)
        return decrypt_group_text(parsed["payload"], self.channels) if parsed else None

    def _upsert_observer(self, observer_id: str, name: Any) -> Optional[dict[str, Any]]:
        if len(observer_id) != 64:
            return None
        known_name = (self.nodes.get(observer_id) or {}).get("name")
        node, changed = self._upsert_node(
            observer_id, is_observer=True, name=None if known_name else (str(name or "").strip() or None)
        )
        return node if changed else None

    @staticmethod
    def _track(buffer: deque[dict[str, Any]], counts: dict[str, int], packet: dict[str, Any]) -> None:
        """Append to a ring buffer, keeping per-hash counts of what's in it (for "heard N×")."""
        if len(buffer) == buffer.maxlen:
            dropped = buffer[0]["hash"]
            remaining = counts.get(dropped, 1) - 1
            if remaining > 0:
                counts[dropped] = remaining
            else:
                counts.pop(dropped, None)
        counts[packet["hash"]] = counts.get(packet["hash"], 0) + 1
        packet["seen_count"] = counts[packet["hash"]]
        buffer.append(packet)

    def _ingest(
        self, rx_log: dict[str, Any], now: Optional[float], observer_id: Optional[str]
    ) -> Optional[dict[str, Any]]:
        raw_hex = str(rx_log.get("payload") or "").strip()
        if not raw_hex:
            # ``raw_hex`` is the whole log frame: SNR byte, RSSI byte, then the packet.
            raw_hex = str(rx_log.get("raw_hex") or "").strip()[4:]
        if not raw_hex or len(raw_hex) % 2 or not HEX_RE.match(raw_hex):
            return None
        decoded = decode_packet(bytes.fromhex(raw_hex))
        if decoded is None:
            return None

        now = time.time() if now is None else now
        packet: dict[str, Any] = {"id": self._next_packet_id, "received_at": now, **decoded, "raw": raw_hex.upper()}
        self._next_packet_id += 1
        for key in ("snr", "rssi"):
            if isinstance(rx_log.get(key), (int, float)):
                packet[key] = rx_log[key]

        changed_nodes: list[dict[str, Any]] = []
        if observer_id:
            packet["source"] = "meshmapper"
            observer_node = self._upsert_observer(observer_id, rx_log.get("observer_name"))
            if observer_node is not None:
                changed_nodes.append(observer_node)
            packet["observer"] = {
                "node_id": observer_id,
                "name": (self.nodes.get(observer_id) or {}).get("name") or rx_log.get("observer_name"),
            }

        # Channel messages: meshcore_py adds the channel and text when it can decrypt
        # a local packet; anything else is decrypted here with the radio's channel keys.
        if packet["payload_type"] == PAYLOAD_TYPE_GRP_TXT:
            channel_name, message = rx_log.get("chan_name"), rx_log.get("message")
            if not message:
                decrypted = self._decrypt_channel_message(bytes.fromhex(raw_hex))
                if decrypted:
                    channel_name, message = decrypted["channel_name"], decrypted["message"]
            if channel_name:
                packet["channel_name"] = str(channel_name)
            if isinstance(message, str) and message:
                sender, sep, body = message.partition(": ")
                if sep and 0 < len(sender) <= 32:
                    packet["sender_name"] = sender
                    packet["message"] = body
                else:
                    packet["message"] = message

        advert = packet.get("advert")
        if packet["payload_type"] == PAYLOAD_TYPE_ADVERT and advert:
            existing = self.nodes.get(advert["public_key"])
            if not existing or (existing.get("last_advert") or 0) <= advert["advert_timestamp"]:
                node, changed = self._upsert_node(
                    advert["public_key"],
                    name=advert.get("name"),
                    type=advert["node_type"],
                    lat=advert.get("lat"),
                    lon=advert.get("lon"),
                    last_advert=advert["advert_timestamp"],
                )
                if changed:
                    changed_nodes.append(node)
        elif packet["payload_type"] == PAYLOAD_TYPE_ANON_REQ and packet.get("src_public_key"):
            node, created = self._upsert_node(packet["src_public_key"])
            if created:
                changed_nodes.append(node)

        packet["path_nodes"] = self._resolve_path(packet["path"], observer_id)
        packet["origin"] = self._resolve_origin(packet)
        if packet["route"] == "flood":
            # The last relay (or the originator, for zero-hop packets) is who was heard.
            packet["heard_from"] = packet["path_nodes"][-1] if packet["path_nodes"] else packet["origin"]
        else:
            packet["heard_from"] = None

        # "Heard directly" signal stats describe this radio's links only.
        heard_id = (packet.get("heard_from") or {}).get("node_id")
        if heard_id and heard_id in self.nodes and not observer_id:
            node, _ = self._upsert_node(heard_id, last_heard=now, last_snr=packet.get("snr"), last_rssi=packet.get("rssi"))
            if node not in changed_nodes:
                changed_nodes.append(node)
        origin_id = (packet.get("origin") or {}).get("node_id")
        if origin_id and origin_id in self.nodes:
            node, _ = self._upsert_node(origin_id, last_seen=now)
            if node not in changed_nodes:
                changed_nodes.append(node)

        if observer_id:
            self._track(self.remote_packets, self._remote_hash_counts, packet)
        else:
            self._track(self.packets, self._hash_counts, packet)
        if packet.get("message"):
            self.messages.append(packet)
        if self._dirty_node_ids is not None:
            self._unsaved_packets.append(packet)

        self._publish({"type": "packet", "packet": packet, "nodes": [dict(node) for node in changed_nodes]})
        return packet

    def snapshot(self) -> dict[str, Any]:
        # Messages still in a packet buffer are already sent there; only send the older ones.
        buffered = {packet["id"] for packet in self.packets} | {packet["id"] for packet in self.remote_packets}
        return {
            "type": "snapshot",
            "self_id": self.self_id,
            "nodes": [dict(node) for node in self.nodes.values()],
            "packets": list(self.packets),
            "remote_packets": list(self.remote_packets),
            "messages": [message for message in self.messages if message["id"] not in buffered],
            "connections": self.status.snapshot() if self.status is not None else [],
        }


# --- HTTP server ---------------------------------------------------------------


_STATUS_TEXT = {200: "OK", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 400: "Bad Request"}


class PacketMapServer:
    def __init__(self, config: PacketMapConfig, state: PacketMapState):
        self.config = config
        self.state = state
        self._server: Optional[asyncio.AbstractServer] = None
        self._index_html = b""
        self._stream_tasks: set[asyncio.Task[Any]] = set()

    @property
    def port(self) -> Optional[int]:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        config_json = json.dumps(self.config.client_config()).replace("</", "<\\/")
        self._index_html = template.replace("/*__MESHGRAM_CONFIG__*/{}", config_json).encode("utf-8")
        self._server = await asyncio.start_server(self._handle_client, self.config.host, self.config.port)
        LOGGER.info("Packet map: serving on http://%s:%s/", self.config.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._stream_tasks):
            task.cancel()
        for task in list(self._stream_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def _authorized(self, headers: dict[str, str]) -> bool:
        if not self.config.password:
            return True
        scheme, _, credentials = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
        except Exception:
            return False
        _, _, password = decoded.partition(":")
        return hmac.compare_digest(password.encode("utf-8"), self.config.password.encode("utf-8"))

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._stream_tasks.add(task)
        try:
            await self._serve_request(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("Packet map: request failed")
        finally:
            if task is not None:
                self._stream_tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        if len(head) > MAX_REQUEST_HEAD_BYTES:
            await self._respond(writer, 400, b"request too large")
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            await self._respond(writer, 400, b"bad request")
            return
        method, target, _ = parts
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        if method not in {"GET", "HEAD"}:
            await self._respond(writer, 405, b"method not allowed")
            return
        if not self._authorized(headers):
            await self._respond(
                writer, 401, b"authentication required", extra_headers={"WWW-Authenticate": 'Basic realm="meshgram"'}
            )
            return

        path = urlsplit(target).path
        body_only = method == "GET"
        if path in {"/", "/index.html"}:
            await self._respond(writer, 200, self._index_html, "text/html; charset=utf-8", body_only)
        elif path == "/api/state":
            await self._respond(writer, 200, json.dumps(self.state.snapshot()).encode("utf-8"), "application/json", body_only)
        elif path == "/healthz":
            await self._respond(writer, 200, b"ok", "text/plain", body_only)
        elif path == "/api/events" and body_only:
            await self._stream_events(writer)
        else:
            await self._respond(writer, 404, b"not found")

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
        include_body: bool = True,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Connection": "close",
            **(extra_headers or {}),
        }
        head = f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'OK')}\r\n"
        head += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
        writer.write(head.encode("latin-1") + (body if include_body else b""))
        await writer.drain()

    async def _stream_events(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n"
            b"X-Accel-Buffering: no\r\n\r\n"
            b"retry: 3000\n\n"
        )
        queue = self.state.subscribe()
        try:
            await self._send_event(writer, self.state.snapshot())
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                if event.get("type") == "overflow":
                    return
                await self._send_event(writer, event)
        finally:
            self.state.unsubscribe(queue)

    @staticmethod
    async def _send_event(writer: asyncio.StreamWriter, event: dict[str, Any]) -> None:
        writer.write(b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n")
        await writer.drain()


# --- Plugin --------------------------------------------------------------------


class PacketMapPlugin(BasePlugin):
    name = "packet_map"

    def __init__(self, settings: Optional[dict[str, Any]] = None):
        super().__init__(settings)
        self.config = PacketMapConfig.from_settings(self.settings)
        self.state = PacketMapState(
            max_packets=self.config.max_packets,
            max_messages=self.config.max_messages,
            max_remote_packets=self.config.max_remote_packets,
        )
        self.server = PacketMapServer(self.config, self.state)
        self._transport: Any = None
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._store: Optional[PacketMapStore] = None
        self._persist_task: Optional[asyncio.Task[None]] = None
        self._enabled = False

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        if context.settings.mesh.backend != MESHCORE_BACKEND:
            LOGGER.error("Packet map disabled: it needs MeshCore RF logs (set mesh.backend to meshcore)")
            return []
        await self._open_store()
        try:
            await self.server.start()
        except OSError as exc:
            LOGGER.error("Packet map disabled: cannot listen on %s:%s (%s)", self.config.host, self.config.port, exc)
            await self._close_store()
            return []
        self._enabled = True
        status = getattr(context, "status", None)
        if status is not None:
            self.state.status = status
            status.add_listener(self.state.apply_status)
        return []

    async def _open_store(self) -> None:
        if self.config.db_path is None:
            LOGGER.info("Packet map: persistence off; history is lost on restart")
            return
        store = PacketMapStore(
            self.config.db_path,
            RetentionLimits(
                max_packets=self.config.max_packets,
                max_remote_packets=self.config.max_remote_packets,
                max_messages=self.config.max_messages,
            ),
        )
        try:
            saved = await asyncio.to_thread(store.open)
        except PacketMapStoreError as exc:
            LOGGER.error("Packet map: persistence off, history is lost on restart (%s)", exc)
            return
        self.state.restore(saved)
        self.state.track_changes()
        self._store = store
        self._persist_task = asyncio.create_task(self._persist_loop(), name="packet-map-persist")
        LOGGER.info(
            "Packet map: restored %d nodes and %d packets from %s", len(saved.nodes), len(saved.packets), store.path
        )

    async def _persist_loop(self) -> None:
        while True:
            await asyncio.sleep(PERSIST_INTERVAL_SECONDS)
            await self._persist()

    async def _persist(self) -> None:
        changes = self.state.drain_changes()
        if changes is None or self._store is None:
            return
        try:
            await asyncio.to_thread(self._store.write, changes)
        except Exception:
            LOGGER.exception("Packet map: failed to save %d packets to %s", len(changes.packets), self._store.path)

    async def _close_store(self) -> None:
        if self._persist_task is not None:
            self._persist_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._persist_task
            self._persist_task = None
        if self._store is not None:
            await self._persist()
            await asyncio.to_thread(self._store.close)
            self._store = None

    async def on_mesh_connected(self, transport: Any, context: PluginContext) -> None:
        if not self._enabled:
            return
        self._transport = transport
        transport.add_rx_log_listener(self.handle_rx_log)
        add_remote_listener = getattr(transport, "add_remote_rx_log_listener", None)
        if callable(add_remote_listener):
            add_remote_listener(self.handle_remote_rx_log)
        self.state.apply_self(transport.device_self_info)
        self._refresh_contacts()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._contact_refresh_loop(), name="packet-map-contacts")

    def _refresh_contacts(self) -> None:
        contacts = getattr(self._transport, "contacts", None)
        if isinstance(contacts, dict):
            self.state.apply_contacts(contacts)
        channels = getattr(self._transport, "channels", None)
        if isinstance(channels, list):
            self.state.channels = channels

    async def _contact_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(CONTACT_REFRESH_SECONDS)
            self._refresh_contacts()

    async def handle_rx_log(self, rx_log: dict[str, Any]) -> None:
        packet = self.state.ingest_rx_log(rx_log)
        if packet is None:
            LOGGER.debug("Packet map: skipping undecodable RX log: %s", rx_log.get("raw_hex"))

    async def handle_remote_rx_log(self, rx_log: dict[str, Any]) -> None:
        if not self._enabled:
            return
        packet = self.state.ingest_remote_rx_log(rx_log)
        if packet is None:
            LOGGER.debug("Packet map: skipping undecodable observer packet from %s", rx_log.get("observer_id"))

    async def on_shutdown(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
            self._refresh_task = None
        if self.state.status is not None:
            self.state.status.remove_listener(self.state.apply_status)
        if self._enabled:
            await self.server.stop()
            self._enabled = False
        await self._close_store()

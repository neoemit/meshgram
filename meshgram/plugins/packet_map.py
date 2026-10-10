"""Live map of MeshCore packet propagation, shown by the web app (``meshgram.web``).

Feeds the web app's Map and Messages views: every node that shares a position
(adverts and radio contacts, repeaters highlighted) and a live list of every RF
packet the radio hears. Repeater hashes in each packet's path are resolved to
known nodes so the route a packet took can be drawn on the map.

Packets heard by other MeshMapper observers (relayed by the meshmapper plugin
through the transport's remote RX log listeners) are shown too, routed to the
observer that heard them, in a buffer of their own. Those from MeshMapper's
live feed arrive as metadata only (no bytes), so they can't be decrypted.

Nodes and packet history are saved to SQLite (see ``packet_map_store``) and
restored on startup, so the map survives restarts and redeploys.

The data reaches open pages through the web app's event stream: a ``map``
section of the snapshot, then ``packet``/``nodes``/``self`` events.

The plugin never emits bridge actions; it only listens to raw RF logs.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from meshgram.config import _as_bool, data_dir
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
from meshgram.types import PluginAction, PluginContext

LOGGER = logging.getLogger(__name__)

HEX_RE = re.compile(r"^[0-9a-fA-F]*$")
# Node types that relay packets and therefore appear in packet paths.
RELAY_NODE_TYPES = {"repeater", "room"}
CONTACT_REFRESH_SECONDS = 30.0
PERSIST_INTERVAL_SECONDS = 5.0
DEFAULT_DB_FILE = "packet_map.sqlite3"
# The web app's snapshot section this plugin fills.
SNAPSHOT_KEY = "map"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class PacketMapConfig:
    max_packets: int = 500
    max_messages: int = 1000
    max_remote_packets: int = 1000
    # Where nodes and packet history are kept across restarts; None keeps them in memory only.
    db_path: Optional[Path] = None

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "PacketMapConfig":
        db_path = None
        if _as_bool(settings.get("persist"), True):
            # A relative path is relative to MESHGRAM_DATA_DIR (an absolute one is used as-is).
            db_path = data_dir() / str(settings.get("db_path") or DEFAULT_DB_FILE)
        return cls(
            max_packets=max(10, _as_int(settings.get("max_packets"), 500)),
            max_messages=max(10, _as_int(settings.get("max_messages"), 1000)),
            max_remote_packets=max(10, _as_int(settings.get("max_remote_packets"), 1000)),
            db_path=db_path,
        )


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
        # Sends live updates to open pages (the web app's EventHub.publish).
        self.publish: Callable[[dict[str, Any]], None] = lambda event: None
        self._hash_counts: dict[str, int] = {}
        self._remote_hash_counts: dict[str, int] = {}
        self._next_packet_id = 1
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
            self.publish({"type": "nodes", "nodes": changed})

    def apply_self(self, info: dict[str, Any]) -> None:
        previous_id = self.self_id
        changed = self.update_self(info)
        if changed or self.self_id != previous_id:
            self.publish({"type": "self", "self_id": self.self_id, "nodes": changed})

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
        key = (
            (packet.get("advert") or {}).get("public_key")
            or packet.get("src_public_key")
            or packet.get("source_public_key")
        )
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
        decoded = rx_log.get("decoded") if observer_id else None
        if isinstance(decoded, dict) and decoded.get("hash"):
            # Another observer's packet known only by its metadata (MeshMapper's live feed).
            raw_hex = ""
        else:
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
        packet: dict[str, Any] = {"id": self._next_packet_id, "received_at": now, **decoded}
        if raw_hex:
            packet["raw"] = raw_hex.upper()
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
            if not message and raw_hex:
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

        # The sender as MeshMapper resolved it (live feed): fills in only what isn't known
        # yet, since adverts heard over the air are more current.
        source = rx_log.get("source_node") if observer_id else None
        source_key = _clean_key(source.get("public_key")) if isinstance(source, dict) else ""
        if len(source_key) == 64:
            known = self.nodes.get(source_key) or {}
            placed = not _has_position(known) and is_valid_position(source.get("lat"), source.get("lon"))
            node, changed = self._upsert_node(
                source_key,
                name=None if known.get("name") else (source.get("name") or None),
                lat=float(source["lat"]) if placed else None,
                lon=float(source["lon"]) if placed else None,
            )
            if changed and node not in changed_nodes:
                changed_nodes.append(node)
            packet["source_public_key"] = source_key

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

        self.publish({"type": "packet", "packet": packet, "nodes": [dict(node) for node in changed_nodes]})
        return packet

    def snapshot(self) -> dict[str, Any]:
        # Messages still in a packet buffer are already sent there; only send the older ones.
        buffered = {packet["id"] for packet in self.packets} | {packet["id"] for packet in self.remote_packets}
        return {
            "self_id": self.self_id,
            "nodes": [dict(node) for node in self.nodes.values()],
            "packets": list(self.packets),
            "remote_packets": list(self.remote_packets),
            "messages": [message for message in self.messages if message["id"] not in buffered],
        }


# --- Plugin --------------------------------------------------------------------


class PacketMapPlugin(BasePlugin):
    name = "packet_map"
    title = "Packet map"
    description = (
        "Feeds the web app's Map and Messages views: nodes with a position, every packet the radio "
        "(and other MeshMapper observers) hears, with routes, and decrypted channel messages."
    )
    settings_schema = {
        "type": "object",
        "properties": {
            "max_packets": {"type": "integer", "minimum": 10, "title": "Packets kept", "default": 500},
            "max_remote_packets": {
                "type": "integer",
                "minimum": 10,
                "title": "Other observers' packets kept",
                "default": 1000,
            },
            "max_messages": {"type": "integer", "minimum": 10, "title": "Messages kept", "default": 1000},
            "persist": {
                "type": "boolean",
                "title": "Keep history across restarts",
                "description": "Saved in an SQLite database in the data directory.",
                "default": True,
            },
            "db_path": {
                "type": "string",
                "minLength": 1,
                "title": "Database file",
                "description": "Relative to the data directory.",
                "default": DEFAULT_DB_FILE,
            },
        },
    }

    def __init__(self, settings: Optional[dict[str, Any]] = None):
        super().__init__(settings)
        self.config = PacketMapConfig.from_settings(self.settings)
        self.state = PacketMapState(
            max_packets=self.config.max_packets,
            max_messages=self.config.max_messages,
            max_remote_packets=self.config.max_remote_packets,
        )
        self._web: Any = None
        self._transport: Any = None
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._store: Optional[PacketMapStore] = None
        self._persist_task: Optional[asyncio.Task[None]] = None

    async def on_startup(self, context: PluginContext) -> list[PluginAction]:
        await self._open_store()
        web = getattr(context, "web", None)
        if web is None:
            LOGGER.warning("Packet map: the web app is off (web.enabled), so the map isn't shown anywhere")
            return []
        self._web = web
        self.state.publish = web.events.publish
        web.events.add_snapshot_provider(SNAPSHOT_KEY, self._snapshot)
        # Open pages show the map now.
        web.events.resync()
        return []

    def _snapshot(self) -> dict[str, Any]:
        return {SNAPSHOT_KEY: self.state.snapshot()}

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
        self._transport = transport
        transport.add_rx_log_listener(self.handle_rx_log)
        transport.add_remote_rx_log_listener(self.handle_remote_rx_log)
        self._refresh_from_radio()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="packet-map-contacts")

    def _refresh_from_radio(self) -> None:
        transport = self._transport
        if transport is None:
            return
        # Its name or position may have changed (from the control panel, say).
        info = transport.device_self_info
        if info:
            self.state.apply_self(info)
        contacts = transport.contacts
        if isinstance(contacts, dict):
            self.state.apply_contacts(contacts)
        channels = transport.channels
        if isinstance(channels, list):
            self.state.channels = channels

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(CONTACT_REFRESH_SECONDS)
            self._refresh_from_radio()

    async def handle_rx_log(self, rx_log: dict[str, Any]) -> None:
        packet = self.state.ingest_rx_log(rx_log)
        if packet is None:
            LOGGER.debug("Packet map: skipping undecodable RX log: %s", rx_log.get("raw_hex"))

    async def handle_remote_rx_log(self, rx_log: dict[str, Any]) -> None:
        packet = self.state.ingest_remote_rx_log(rx_log)
        if packet is None:
            LOGGER.debug("Packet map: skipping undecodable observer packet from %s", rx_log.get("observer_id"))

    async def on_shutdown(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
            self._refresh_task = None
        if self._transport is not None:
            self._transport.remove_rx_log_listener(self.handle_rx_log)
            self._transport.remove_remote_rx_log_listener(self.handle_remote_rx_log)
            self._transport = None
        if self._web is not None:
            self._web.events.remove_snapshot_provider(SNAPSHOT_KEY)
            self.state.publish = lambda event: None
            # Open pages drop the map now.
            self._web.events.resync()
            self._web = None
        await self._close_store()

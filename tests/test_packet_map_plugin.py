import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from meshgram.config import MESHCORE_BACKEND, MESHTASTIC_BACKEND, PluginConfig
from meshgram.meshcore_packets import decode_advert, decode_packet, decrypt_group_text
from meshgram.plugin import load_plugins
from meshgram.plugins.packet_map import PacketMapConfig, PacketMapPlugin, PacketMapServer, PacketMapState
from meshgram.plugins.packet_map_store import (
    SCHEMA_VERSION,
    PacketMapStore,
    PacketMapStoreError,
    RetentionLimits,
)
from meshgram.status import StatusRegistry

SELF_KEY = "AA" * 32
REPEATER_KEY = "B1" + "11" * 31
OTHER_REPEATER_KEY = "B1" + "22" * 31
COMPANION_KEY = "C3" * 32
OBSERVER_KEY = "D0" * 32
CHANNEL_SECRET = bytes(range(16))
CHANNEL = {"name": "#ping", "secret": CHANNEL_SECRET, "hash": hashlib.sha256(CHANNEL_SECRET).hexdigest()[:2]}


def _channel_payload(text: str, secret: bytes = CHANNEL_SECRET, timestamp: int = 1234) -> str:
    """GRP_TXT payload as MeshCore builds it: hash(1) || HMAC(2) || AES-128-ECB(timestamp, flags, text)."""
    from Crypto.Cipher import AES

    plain = timestamp.to_bytes(4, "little") + b"\x00" + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    ciphertext = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2]
    return (hashlib.sha256(secret).digest()[:1] + mac + ciphertext).hex()


def _advert(public_key: str, node_type: int, name: str = "", lat: float = None, lon: float = None, timestamp: int = 1000) -> str:
    flags = node_type
    appdata = b""
    if lat is not None:
        flags |= 0x10
        appdata += int(lat * 1_000_000).to_bytes(4, "little", signed=True)
        appdata += int(lon * 1_000_000).to_bytes(4, "little", signed=True)
    if name:
        flags |= 0x80
        appdata += name.encode("utf-8")
    payload = bytes.fromhex(public_key) + timestamp.to_bytes(4, "little") + b"\x00" * 64 + bytes([flags]) + appdata
    return payload.hex()


def _flood(payload_type: int, path: list[str], payload_hex: str) -> str:
    header = (payload_type << 2) | 0x01
    return f"{header:02X}{len(path):02X}" + "".join(path) + payload_hex


def _context(backend: str = MESHCORE_BACKEND) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(mesh=SimpleNamespace(backend=backend)))


class DecodeTests(unittest.TestCase):
    def test_decodes_advert_with_location_and_name(self):
        advert = decode_advert(bytes.fromhex(_advert(REPEATER_KEY, 2, "Hilltop", 45.5, -73.25, timestamp=1234)))
        self.assertEqual(
            advert,
            {
                "public_key": REPEATER_KEY,
                "advert_timestamp": 1234,
                "node_type": "repeater",
                "lat": 45.5,
                "lon": -73.25,
                "name": "Hilltop",
            },
        )

    def test_zero_location_is_ignored(self):
        advert = decode_advert(bytes.fromhex(_advert(COMPANION_KEY, 1, "Phone", 0, 0)))
        self.assertNotIn("lat", advert)
        self.assertEqual(advert["name"], "Phone")

    def test_decodes_addressed_payload_and_path(self):
        decoded = decode_packet(bytes.fromhex(_flood(2, ["B1", "D4"], "C3AA" + "00112233")))
        self.assertEqual(decoded["payload_type_name"], "TXT_MSG")
        self.assertEqual(decoded["route"], "flood")
        self.assertEqual(decoded["path"], ["B1", "D4"])
        self.assertEqual(decoded["hops"], 2)
        self.assertEqual((decoded["dest_hash"], decoded["src_hash"]), ("C3", "AA"))

    def test_transport_direct_packet(self):
        decoded = decode_packet(bytes.fromhex("0B" + "01020304" + "42" + "AAAABBBB" + "CCDD"))
        self.assertEqual(decoded["route_type_name"], "TRANSPORT_DIRECT")
        self.assertEqual(decoded["route"], "direct")
        self.assertEqual(decoded["transport_codes"], ["0102", "0304"])
        self.assertEqual(decoded["path"], ["AAAA", "BBBB"])

    def test_trace_path_is_snr_list(self):
        decoded = decode_packet(bytes.fromhex("2602" + "14F8" + "01020304" + "00000000" + "00" + "B1"))
        self.assertEqual(decoded["payload_type_name"], "TRACE")
        self.assertEqual(decoded["trace_snrs"], [5.0, -2.0])
        self.assertEqual(decoded["path"], [])

    def test_decrypts_channel_text_with_matching_secret(self):
        payload = bytes.fromhex(_channel_payload("Phone: hello mesh"))
        self.assertEqual(
            decrypt_group_text(payload, [CHANNEL]),
            {"channel_name": "#ping", "message": "Phone: hello mesh", "sender_timestamp": 1234},
        )
        other = {**CHANNEL, "secret": bytes(16 * [7])}
        self.assertIsNone(decrypt_group_text(payload, [other]))  # MAC doesn't match
        self.assertIsNone(decrypt_group_text(payload[:-1], [CHANNEL]))  # not whole AES blocks

    def test_truncated_packet(self):
        self.assertIsNone(decode_packet(bytes.fromhex("1505aa")))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = PacketMapState(max_packets=3)
        self.state.update_self({"public_key": SELF_KEY.lower(), "name": "Base", "adv_lat": 45.0, "adv_lon": -73.0})

    def test_self_node(self):
        node = self.state.nodes[SELF_KEY]
        self.assertTrue(node["is_self"])
        self.assertEqual((node["name"], node["lat"], node["lon"]), ("Base", 45.0, -73.0))

    def test_advert_creates_node_and_marks_origin(self):
        packet = self.state.ingest_rx_log(
            {"payload": _flood(4, [], _advert(REPEATER_KEY, 2, "Hilltop", 45.1, -73.1)), "snr": 7.5, "rssi": -80},
            now=100.0,
        )
        node = self.state.nodes[REPEATER_KEY]
        self.assertEqual((node["name"], node["type"], node["lat"]), ("Hilltop", "repeater", 45.1))
        self.assertEqual(packet["origin"]["node_id"], REPEATER_KEY)
        # Zero-hop flood: we heard the originator directly.
        self.assertEqual(packet["heard_from"]["node_id"], REPEATER_KEY)
        self.assertEqual((node["last_snr"], node["last_rssi"], node["last_heard"]), (7.5, -80, 100.0))
        self.assertEqual((packet["snr"], packet["rssi"]), (7.5, -80))

    def test_older_advert_does_not_override_position(self):
        self.state.ingest_rx_log({"payload": _flood(4, [], _advert(REPEATER_KEY, 2, "New", 45.2, -73.2, timestamp=2000))})
        self.state.ingest_rx_log({"payload": _flood(4, ["B1"], _advert(REPEATER_KEY, 2, "Old", 40.0, -70.0, timestamp=1000))})
        self.assertEqual(self.state.nodes[REPEATER_KEY]["name"], "New")
        self.assertEqual(self.state.nodes[REPEATER_KEY]["lat"], 45.2)

    def test_contacts_feed_nodes(self):
        changed = self.state.update_contacts(
            {
                REPEATER_KEY.lower(): {"public_key": REPEATER_KEY.lower(), "type": 2, "adv_name": "Hilltop", "adv_lat": 45.1, "adv_lon": -73.1, "last_advert": 5},
                COMPANION_KEY.lower(): {"public_key": COMPANION_KEY.lower(), "type": 1, "adv_name": "Phone", "adv_lat": 0.0, "adv_lon": 0.0},
            }
        )
        self.assertEqual(len(changed), 2)
        self.assertEqual(self.state.nodes[REPEATER_KEY]["type"], "repeater")
        self.assertIsNone(self.state.nodes[COMPANION_KEY]["lat"])
        # No changes → nothing reported.
        self.assertEqual(self.state.update_contacts({REPEATER_KEY: {"public_key": REPEATER_KEY, "type": 2, "adv_name": "Hilltop", "adv_lat": 45.1, "adv_lon": -73.1, "last_advert": 5}}), [])

    def test_ambiguous_path_hash_picks_nearest_repeater(self):
        self.state.update_contacts(
            {
                REPEATER_KEY: {"type": 2, "adv_name": "Near", "adv_lat": 45.01, "adv_lon": -73.01},
                OTHER_REPEATER_KEY: {"type": 2, "adv_name": "Far", "adv_lat": 48.0, "adv_lon": -80.0},
                COMPANION_KEY: {"type": 1, "adv_name": "Phone", "adv_lat": 45.2, "adv_lon": -73.2},
            }
        )
        packet = self.state.ingest_rx_log({"payload": _flood(2, ["B1"], "AAC3" + "00112233"), "snr": 3.0})
        self.assertEqual(packet["path_nodes"], [{"hash": "B1", "node_id": REPEATER_KEY, "name": "Near", "candidates": 2}])
        self.assertEqual(packet["heard_from"]["name"], "Near")
        self.assertEqual(packet["origin"]["node_id"], COMPANION_KEY)

    def test_unknown_hop_stays_unresolved(self):
        packet = self.state.ingest_rx_log({"payload": _flood(5, ["EE"], "11" + "2233" + "445566")})
        self.assertEqual(packet["path_nodes"][0]["node_id"], None)
        self.assertEqual(packet["channel_hash"], "11")

    def test_channel_message_extras_from_meshcore_py(self):
        self.state.update_contacts({COMPANION_KEY: {"type": 1, "adv_name": "Phone", "adv_lat": 45.2, "adv_lon": -73.2}})
        packet = self.state.ingest_rx_log(
            {"payload": _flood(5, [], "11" + "2233" + "44"), "chan_name": "Public", "message": "Phone: hello there", "pkt_payload": b"\x00"}
        )
        self.assertEqual((packet["channel_name"], packet["sender_name"], packet["message"]), ("Public", "Phone", "hello there"))
        self.assertEqual(packet["origin"]["node_id"], COMPANION_KEY)
        json.dumps(packet)  # must stay JSON-serialisable

    def test_falls_back_to_raw_hex_and_skips_garbage(self):
        packet = self.state.ingest_rx_log({"raw_hex": "199f" + "1500aabb"})
        self.assertEqual(packet["raw"], "1500AABB")
        self.assertIsNone(self.state.ingest_rx_log({"payload": "zz"}))
        self.assertIsNone(self.state.ingest_rx_log({}))

    def test_ring_buffer_and_duplicate_counts(self):
        raw = _flood(5, [], "11" + "2233" + "44")
        counts = [self.state.ingest_rx_log({"payload": raw})["seen_count"] for _ in range(3)]
        self.assertEqual(counts, [1, 2, 3])
        self.state.ingest_rx_log({"payload": _flood(5, [], "99" + "2233" + "44")})
        self.assertEqual(len(self.state.packets), 3)
        # Two of the three earlier copies have been evicted by now.
        self.assertEqual(self.state.ingest_rx_log({"payload": raw})["seen_count"], 2)
        self.assertEqual([p["id"] for p in self.state.snapshot()["packets"]], [3, 4, 5])

    def test_messages_outlive_packet_buffer(self):
        for i in range(3):
            self.state.ingest_rx_log({"payload": _flood(5, [], f"1{i}" + "2233" + "44"), "chan_name": "Public", "message": f"Phone: hi {i}"})
        for _ in range(2):
            self.state.ingest_rx_log({"payload": _flood(4, [], _advert(REPEATER_KEY, 2, "Hilltop", 45.1, -73.1))})
        snapshot = self.state.snapshot()
        self.assertEqual([p["id"] for p in snapshot["packets"]], [3, 4, 5])
        # Message 3 is still in the packet buffer, so only the two evicted ones are added.
        self.assertEqual([m["message"] for m in snapshot["messages"]], ["hi 0", "hi 1"])
        self.assertEqual(len(self.state.messages), 3)

    def test_other_observers_packets(self):
        self.state.update_contacts(
            {
                OBSERVER_KEY: {"type": 2, "adv_name": "Hill Observer", "adv_lat": 46.0, "adv_lon": -74.0},
                REPEATER_KEY: {"type": 2, "adv_name": "Near observer", "adv_lat": 46.01, "adv_lon": -74.01},
                OTHER_REPEATER_KEY: {"type": 2, "adv_name": "Near us", "adv_lat": 45.01, "adv_lon": -73.01},
            }
        )
        self.state.channels = [CHANNEL]
        raw = _flood(5, ["B1"], _channel_payload("Phone: hi from the hill"))
        packet = self.state.ingest_remote_rx_log(
            {"payload": raw, "snr": 4.5, "rssi": -90, "observer_id": OBSERVER_KEY.lower(), "observer_name": "Hill"},
            now=50.0,
        )
        self.assertEqual(packet["source"], "meshmapper")
        self.assertEqual(packet["observer"], {"node_id": OBSERVER_KEY, "name": "Hill Observer"})
        # Decrypted with the radio's channel keys.
        self.assertEqual((packet["channel_name"], packet["sender_name"], packet["message"]), ("#ping", "Phone", "hi from the hill"))
        # The ambiguous "B1" hop is resolved near the observer that heard it, not near us.
        self.assertEqual(packet["path_nodes"][0]["node_id"], REPEATER_KEY)
        self.assertTrue(self.state.nodes[OBSERVER_KEY]["is_observer"])
        # "Heard directly" stats are only about this radio's links.
        self.assertNotIn("last_heard", self.state.nodes[REPEATER_KEY])
        # Kept apart from this radio's packets, and duplicates are counted separately.
        self.assertEqual((len(self.state.packets), len(self.state.remote_packets)), (0, 1))
        self.assertEqual(self.state.ingest_rx_log({"payload": raw})["seen_count"], 1)

        # Unknown observers become nodes named after their MeshMapper origin.
        other = "E0" * 32
        self.state.ingest_remote_rx_log({"payload": raw, "observer_id": other, "observer_name": "Valley"})
        self.assertEqual(self.state.remote_packets[-1]["seen_count"], 2)
        self.assertEqual((self.state.nodes[other]["name"], self.state.nodes[other]["is_observer"]), ("Valley", True))
        # Our own uploads coming back are ignored.
        self.assertIsNone(self.state.ingest_remote_rx_log({"payload": raw, "observer_id": SELF_KEY}))

        snapshot = self.state.snapshot()
        self.assertEqual([p["id"] for p in snapshot["remote_packets"]], [1, 3])
        self.assertEqual(snapshot["messages"], [])  # all still in a packet buffer
        self.assertEqual(len(self.state.messages), 3)

    def test_subscribers_receive_packets(self):
        queue = self.state.subscribe()
        self.state.ingest_rx_log({"payload": _flood(4, [], _advert(REPEATER_KEY, 2, "Hilltop", 45.1, -73.1))})
        event = queue.get_nowait()
        self.assertEqual(event["type"], "packet")
        self.assertEqual(event["nodes"][0]["id"], REPEATER_KEY)
        self.state.unsubscribe(queue)
        self.state.ingest_rx_log({"payload": _flood(4, [], _advert(REPEATER_KEY, 2, "Hilltop", 45.1, -73.1))})
        self.assertTrue(queue.empty())


async def _http(port: int, path: str, headers: str = "") -> tuple[str, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{headers}\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode().split("\r\n")[0], body


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, **settings) -> PacketMapServer:
        state = PacketMapState()
        state.update_self({"public_key": SELF_KEY, "name": "Base"})
        server = PacketMapServer(PacketMapConfig.from_settings({"port": 0, **settings}), state)
        await server.start()
        self.addAsyncCleanup(server.stop)
        return server

    async def test_serves_page_and_state(self):
        server = await self._start(title="My </script> map")
        status, body = await _http(server.port, "/")
        self.assertIn("200", status)
        self.assertIn(b"leaflet", body)
        self.assertIn(b"My <\\/script> map", body)
        self.assertNotIn(b"__MESHGRAM_CONFIG__", body)

        status, body = await _http(server.port, "/api/state")
        self.assertEqual(json.loads(body)["self_id"], SELF_KEY)
        status, _ = await _http(server.port, "/nope")
        self.assertIn("404", status)

    async def test_password_protection(self):
        server = await self._start(password="s3cret")
        status, _ = await _http(server.port, "/api/state")
        self.assertIn("401", status)
        token = base64.b64encode(b"anyone:s3cret").decode()
        status, _ = await _http(server.port, "/api/state", f"Authorization: Basic {token}\r\n")
        self.assertIn("200", status)

    async def test_event_stream_sends_snapshot_then_packets(self):
        server = await self._start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()

        async def next_event() -> dict:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if line.startswith(b"data: "):
                    return json.loads(line[6:])

        self.assertEqual((await next_event())["type"], "snapshot")
        server.state.ingest_rx_log({"payload": _flood(5, [], "11" + "2233" + "44")})
        event = await next_event()
        self.assertEqual((event["type"], event["packet"]["payload_type_name"]), ("packet", "GRP_TXT"))
        writer.close()


class _FakeTransport:
    def __init__(self):
        self.listeners = []
        self.device_self_info = {"public_key": SELF_KEY, "name": "Base", "adv_lat": 45.0, "adv_lon": -73.0}
        self.contacts = {REPEATER_KEY: {"type": 2, "adv_name": "Hilltop", "adv_lat": 45.1, "adv_lon": -73.1}}
        self.channels = [CHANNEL]
        self.remote_listeners = []

    def add_rx_log_listener(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def add_remote_rx_log_listener(self, listener):
        if listener not in self.remote_listeners:
            self.remote_listeners.append(listener)


class _DataDirMixin:
    def _use_temp_data_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(tmp.name)


class PluginTests(_DataDirMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data_dir = self._use_temp_data_dir()

    def test_plugin_is_registered(self):
        plugins = load_plugins([PluginConfig(name="packet_map", enabled=True, settings={})])
        self.assertIsInstance(plugins[0].instance, PacketMapPlugin)

    async def test_disabled_on_meshtastic(self):
        plugin = PacketMapPlugin({"port": 0})
        await plugin.on_startup(_context(MESHTASTIC_BACKEND))
        transport = _FakeTransport()
        await plugin.on_mesh_connected(transport, _context(MESHTASTIC_BACKEND))
        self.assertEqual(transport.listeners, [])
        self.assertIsNone(plugin.server.port)

    async def test_lifecycle(self):
        plugin = PacketMapPlugin({"port": 0})
        await plugin.on_startup(_context())
        self.addAsyncCleanup(plugin.on_shutdown)
        transport = _FakeTransport()
        await plugin.on_mesh_connected(transport, _context())
        await plugin.on_mesh_connected(transport, _context())  # reconnect: no duplicate listener
        self.assertEqual(transport.listeners, [plugin.handle_rx_log])
        self.assertEqual(plugin.state.self_id, SELF_KEY)
        self.assertEqual(plugin.state.nodes[REPEATER_KEY]["name"], "Hilltop")

        await transport.listeners[0]({"payload": _flood(2, ["B1"], "AAC3" + "0011"), "snr": 2.0})
        self.assertEqual(plugin.state.packets[-1]["path_nodes"][0]["name"], "Hilltop")

        # Packets other MeshMapper observers heard arrive through the transport too.
        self.assertEqual(transport.remote_listeners, [plugin.handle_remote_rx_log])
        await transport.remote_listeners[0](
            {"payload": _flood(5, [], _channel_payload("Phone: hi")), "observer_id": OBSERVER_KEY, "observer_name": "Hill"}
        )
        self.assertEqual(plugin.state.remote_packets[-1]["message"], "hi")

        await plugin.on_shutdown()
        self.assertIsNone(plugin.server.port)

    async def test_connection_status_is_streamed(self):
        status = StatusRegistry()
        status.set_state("radio", "connected", "meshcore serial", label="Radio")
        context = _context()
        context.status = status
        plugin = PacketMapPlugin({"port": 0})
        await plugin.on_startup(context)
        self.addAsyncCleanup(plugin.on_shutdown)
        self.assertEqual([entry["key"] for entry in plugin.state.snapshot()["connections"]], ["radio"])

        queue = plugin.state.subscribe()
        status.set_state("mqtt_publish", "disconnected", "refused", label="MQTT")
        event = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(event["type"], "connection")
        self.assertEqual((event["connection"]["key"], event["connection"]["state"]), ("mqtt_publish", "disconnected"))

        await plugin.on_shutdown()
        status.set_state("radio", "disconnected")
        await asyncio.sleep(0)
        self.assertTrue(queue.empty())


class StoreTests(_DataDirMixin, unittest.TestCase):
    def setUp(self):
        self.path = self._use_temp_data_dir() / "map.sqlite3"

    def _store(self, max_packets=3, max_remote_packets=3, max_messages=3) -> PacketMapStore:
        store = PacketMapStore(self.path, RetentionLimits(max_packets, max_remote_packets, max_messages))
        self.addCleanup(store.close)
        return store

    def _ingest_some(self, state: PacketMapState) -> None:
        state.update_self({"public_key": SELF_KEY, "name": "Base", "adv_lat": 45.0, "adv_lon": -73.0})
        state.channels = [CHANNEL]
        state.ingest_rx_log({"payload": _flood(4, [], _advert(REPEATER_KEY, 2, "Hilltop", 45.1, -73.1))}, now=1.0)
        state.ingest_rx_log({"payload": _flood(5, ["B1"], _channel_payload("Phone: hi"))}, now=2.0)
        state.ingest_rx_log({"payload": _flood(5, [], _channel_payload("Phone: hi"))}, now=3.0)
        state.ingest_remote_rx_log(
            {"payload": _flood(2, [], "C3AA" + "0011"), "observer_id": OBSERVER_KEY, "observer_name": "Hill"}, now=4.0
        )

    def test_round_trip(self):
        store = self._store()
        state = PacketMapState()
        state.restore(store.open())
        state.track_changes()
        self._ingest_some(state)
        store.write(state.drain_changes())
        self.assertIsNone(state.drain_changes())
        store.close()

        restored = PacketMapState()
        restored.restore(self._store().open())
        self.assertEqual(restored.self_id, SELF_KEY)
        self.assertEqual(restored.nodes, state.nodes)
        self.assertEqual(list(restored.packets), list(state.packets))
        self.assertEqual(list(restored.remote_packets), list(state.remote_packets))
        self.assertEqual([m["id"] for m in restored.messages], [m["id"] for m in state.messages])
        self.assertEqual(restored.packets[-1]["seen_count"], 2)
        self.assertEqual(restored.snapshot(), state.snapshot())
        # IDs keep counting up, so the page never confuses old and new packets.
        packet = restored.ingest_rx_log({"payload": _flood(5, [], "11" + "2233" + "44")})
        self.assertEqual(packet["id"], 5)
        # Repeat sightings still count the restored copies.
        again = restored.ingest_rx_log({"payload": _flood(5, ["B1", "B1"], _channel_payload("Phone: hi"))})
        self.assertEqual(again["seen_count"], 3)

    def test_untracked_state_records_nothing(self):
        state = PacketMapState()
        self._ingest_some(state)
        self.assertIsNone(state.drain_changes())
        self.assertEqual(state._unsaved_packets, [])

    def test_retention_follows_buffer_limits(self):
        store = self._store(max_packets=2, max_remote_packets=1, max_messages=2)
        state = PacketMapState(max_packets=2, max_remote_packets=1, max_messages=2)
        state.restore(store.open())
        state.track_changes()
        state.channels = [CHANNEL]
        state.ingest_rx_log({"payload": _flood(5, [], _channel_payload("A: old message"))})
        for _ in range(3):
            state.ingest_rx_log({"payload": _flood(2, [], "C3AA" + "0011")})
        store.write(state.drain_changes())

        ids = [row[0] for row in sqlite3.connect(self.path).execute("SELECT id FROM packets ORDER BY id")]
        # The message outlives the packet buffer; the oldest plain packet is gone.
        self.assertEqual(ids, [1, 3, 4])
        restored = PacketMapState(max_packets=2, max_remote_packets=1, max_messages=2)
        restored.restore(self._store(max_packets=2, max_remote_packets=1, max_messages=2).open())
        self.assertEqual([p["id"] for p in restored.packets], [3, 4])
        self.assertEqual([m["id"] for m in restored.messages], [1])

    def test_lowered_limits_prune_on_open(self):
        store = self._store(max_packets=10)
        state = PacketMapState(max_packets=10)
        state.restore(store.open())
        state.track_changes()
        for _ in range(5):
            state.ingest_rx_log({"payload": _flood(2, [], "C3AA" + "0011")})
        store.write(state.drain_changes())
        store.close()
        self.assertEqual(len(self._store(max_packets=2).open().packets), 2)

    def test_corrupt_database_is_moved_aside(self):
        self.path.write_bytes(b"this is not a sqlite database" * 100)
        saved = self._store().open()
        self.assertEqual((saved.nodes, saved.packets), ([], []))
        self.assertEqual(len(list(self.path.parent.glob("map.sqlite3.corrupt-*"))), 1)

    def test_newer_schema_is_left_alone(self):
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(PacketMapStoreError):
            self._store().open()
        self.assertTrue(self.path.exists())


class PersistenceConfigTests(unittest.TestCase):
    def test_db_path_defaults_to_data_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESHGRAM_DATA_DIR", None)
            os.environ.pop("PACKET_MAP_DB_PATH", None)
            self.assertEqual(PacketMapConfig.from_settings({}).db_path, Path("data/packet_map.sqlite3"))
            with mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": "/var/lib/meshgram"}):
                self.assertEqual(PacketMapConfig.from_settings({}).db_path, Path("/var/lib/meshgram/packet_map.sqlite3"))
                self.assertEqual(PacketMapConfig.from_settings({"db_path": "/srv/map.db"}).db_path, Path("/srv/map.db"))
            self.assertIsNone(PacketMapConfig.from_settings({"persist": False}).db_path)
            self.assertIsNone(PacketMapConfig.from_settings({"persist": "false"}).db_path)


class PluginPersistenceTests(_DataDirMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data_dir = self._use_temp_data_dir()

    async def test_history_survives_restart(self):
        plugin = PacketMapPlugin({"port": 0})
        await plugin.on_startup(_context())
        transport = _FakeTransport()
        await plugin.on_mesh_connected(transport, _context())
        await plugin.handle_rx_log({"payload": _flood(4, [], _advert(COMPANION_KEY, 1, "Phone", 45.2, -73.2))})
        await plugin.handle_remote_rx_log(
            {"payload": _flood(5, [], _channel_payload("Phone: hi")), "observer_id": OBSERVER_KEY, "observer_name": "Hill"}
        )
        snapshot = plugin.state.snapshot()
        await plugin.on_shutdown()
        self.assertTrue((self.data_dir / "packet_map.sqlite3").exists())

        restarted = PacketMapPlugin({"port": 0})
        await restarted.on_startup(_context())
        self.addAsyncCleanup(restarted.on_shutdown)
        self.assertEqual(restarted.state.snapshot(), snapshot)
        self.assertEqual(restarted.state.nodes[COMPANION_KEY]["name"], "Phone")
        self.assertEqual(restarted.state.remote_packets[-1]["message"], "hi")

    async def test_persist_false_writes_nothing(self):
        plugin = PacketMapPlugin({"port": 0, "persist": False})
        await plugin.on_startup(_context())
        await plugin.handle_rx_log({"payload": _flood(2, [], "C3AA" + "0011")})
        await plugin.on_shutdown()
        self.assertEqual(list(self.data_dir.iterdir()), [])

    async def test_unusable_data_dir_keeps_map_running(self):
        blocker = self.data_dir / "file"
        blocker.write_text("x")
        plugin = PacketMapPlugin({"port": 0, "db_path": str(blocker / "map.sqlite3")})
        with self.assertLogs("meshgram.plugins.packet_map", "ERROR"):
            await plugin.on_startup(_context())
        self.addAsyncCleanup(plugin.on_shutdown)
        self.assertIsNotNone(plugin.server.port)
        await plugin.handle_rx_log({"payload": _flood(2, [], "C3AA" + "0011")})
        self.assertEqual(len(plugin.state.packets), 1)


if __name__ == "__main__":
    unittest.main()

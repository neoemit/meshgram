import asyncio
import base64
import hashlib
import json
import os
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

from meshgram._ed25519 import public_key_from_expanded, sign_with_expanded_key
from meshgram.config import MeshgramSettings
from meshgram.plugin import load_plugin_class
from meshgram.plugins.meshmapper import (
    CLIENT_VERSION,
    MeshMapperConfig,
    MeshMapperLiveFeed,
    MeshMapperPlugin,
    MeshMapperSubscriber,
    MeshMapperUploader,
    build_packet_message,
    feed_backoff_seconds,
    packet_hash,
    parse_feed_observation,
    parse_observer_packet,
)
from meshgram.status import StatusRegistry
from meshgram.types import PluginContext

# RFC 8032 test vector 1, converted to MeshCore's 64-byte (scalar || prefix) key format.
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUBLIC_KEY = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC_SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


def _expanded_key(seed: bytes) -> bytes:
    digest = bytearray(hashlib.sha512(seed).digest())
    digest[0] &= 248
    digest[31] &= 127
    digest[31] |= 64
    return bytes(digest)


RFC_PRIVATE_KEY = _expanded_key(RFC_SEED)
PUBKEY_HEX = RFC_PUBLIC_KEY.hex().upper()
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def _b64url_json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vector_with_meshcore_key_format(self):
        self.assertEqual(public_key_from_expanded(RFC_PRIVATE_KEY), RFC_PUBLIC_KEY)
        self.assertEqual(sign_with_expanded_key(b"", RFC_PRIVATE_KEY, RFC_PUBLIC_KEY), RFC_SIGNATURE)


class PacketFormattingTests(unittest.TestCase):
    def test_flood_group_text_packet(self):
        # header 0x15 = FLOOD route, payload type 5 (GRP_TXT); 2 one-byte path hashes.
        raw_hex = "1502a1b2" + "11223344"
        message = build_packet_message(
            {"payload": raw_hex, "snr": 6.25, "rssi": -97, "raw_hex": "199f" + raw_hex},
            origin="Gateway",
            origin_id=PUBKEY_HEX,
            now=NOW,
        )

        self.assertEqual(
            message,
            {
                "origin": "Gateway",
                "origin_id": PUBKEY_HEX,
                "timestamp": "2026-03-04T05:06:07+00:00",
                "type": "PACKET",
                "direction": "rx",
                "time": "05:06:07",
                "date": "4/3/2026",
                "len": "8",
                "packet_type": "5",
                "route": "F",
                "payload_len": "4",
                "raw": "1502A1B211223344",
                "hash": packet_hash(5, 0x02, bytes.fromhex("11223344")),
                "SNR": "6.25",
                "RSSI": "-97",
            },
        )

    def test_hash_matches_firmware_algorithm(self):
        expected = hashlib.sha256(bytes([5]) + bytes.fromhex("11223344")).hexdigest()[:16].upper()
        self.assertEqual(packet_hash(5, 0x02, bytes.fromhex("11223344")), expected)

    def test_trace_hash_includes_path_len_as_uint16(self):
        expected = hashlib.sha256(bytes([9]) + b"\x03\x00" + b"\xaa").hexdigest()[:16].upper()
        self.assertEqual(packet_hash(9, 3, b"\xaa"), expected)

    def test_direct_packet_includes_path_and_transport_codes_are_skipped(self):
        # header 0x0B = TRANSPORT_DIRECT, payload type 2 (TXT_MSG); 4 transport bytes;
        # path_len 0x42 = 2-byte hashes x 2 hops.
        raw_hex = "0B" + "01020304" + "42" + "AAAABBBB" + "CCDD"
        message = build_packet_message({"payload": raw_hex}, "Gateway", PUBKEY_HEX, now=NOW)

        assert message is not None
        self.assertEqual(message["route"], "D")
        self.assertEqual(message["packet_type"], "2")
        self.assertEqual(message["payload_len"], "2")
        self.assertEqual(message["path"], "AAAA,BBBB")
        self.assertNotIn("SNR", message)

    def test_falls_back_to_raw_hex_without_snr_rssi_prefix(self):
        message = build_packet_message({"raw_hex": "199f" + "1500aabb"}, "Gateway", PUBKEY_HEX, now=NOW)
        assert message is not None
        self.assertEqual(message["raw"], "1500AABB")

    def test_truncated_packet_is_skipped(self):
        self.assertIsNone(build_packet_message({"payload": "15"}, "Gateway", PUBKEY_HEX))
        self.assertIsNone(build_packet_message({"payload": "1505aa"}, "Gateway", PUBKEY_HEX))
        self.assertIsNone(build_packet_message({"payload": "zz"}, "Gateway", PUBKEY_HEX))


class ObserverPacketTests(unittest.TestCase):
    OTHER = "AB" * 32

    def _message(self, **overrides):
        message = {"origin": "Hilltop obs", "origin_id": self.OTHER, "type": "PACKET", "direction": "rx",
                   "raw": "1500aabb", "SNR": "6.25", "RSSI": "-97"}
        message.update(overrides)
        return json.dumps(message).encode()

    def test_other_observer_packet_becomes_rx_log(self):
        observation = parse_observer_packet(f"meshcore/YOW/{self.OTHER}/packets", self._message(), PUBKEY_HEX)
        self.assertEqual(
            observation,
            {"payload": "1500AABB", "observer_id": self.OTHER, "observer_name": "Hilltop obs", "snr": 6.25, "rssi": -97.0},
        )

    def test_skips_own_uploads_transmissions_and_garbage(self):
        topic = f"meshcore/YOW/{self.OTHER}/packets"
        self.assertIsNone(parse_observer_packet(f"meshcore/YOW/{PUBKEY_HEX}/packets", self._message(origin_id=PUBKEY_HEX), PUBKEY_HEX))
        self.assertIsNone(parse_observer_packet(topic, self._message(direction="tx"), PUBKEY_HEX))
        self.assertIsNone(parse_observer_packet(topic, self._message(type="STATUS"), PUBKEY_HEX))
        self.assertIsNone(parse_observer_packet(topic, self._message(raw="zz"), PUBKEY_HEX))
        self.assertIsNone(parse_observer_packet(topic, b"not json", PUBKEY_HEX))


def _feed_data(observer_key: str, payload_type: int = 4, **observation) -> dict:
    """A live feed ``packetObservation`` payload, as MeshMapper sends it."""
    data = {
        "packetHash": "73408c45e49bd15f",
        "packet": {"payloadType": payload_type, "payloadTypeName": "ADVERT", "routeType": 1, "routeTypeName": "FLOOD"},
        "observation": {
            "observerName": "Hilltop obs",
            "observerPublicKey": observer_key.lower(),
            "iata": "YOW",
            "rssi": -97,
            "snr": 6.25,
            "pathBytes": "b2be3dc8",
            "pathLength": {"raw": "42", "hashSize": 2, "hopCount": 2},
            "resolvedSource": {
                "confidence": "high",
                "nodes": [{"name": "Solar", "publicKey": "cd" * 32, "latitude": 45.5, "longitude": -75.5}],
            },
        },
    }
    data["observation"].update(observation)
    return data


class FeedObservationTests(unittest.TestCase):
    OTHER = "AB" * 32

    def test_observation_becomes_decoded_rx_log(self):
        self.assertEqual(
            parse_feed_observation(_feed_data(self.OTHER), PUBKEY_HEX),
            {
                "decoded": {
                    "hash": "73408C45E49BD15F",
                    "payload_type": 4,
                    "payload_type_name": "ADVERT",
                    "route_type": 1,
                    "route_type_name": "FLOOD",
                    "route": "flood",
                    "path": ["B2BE", "3DC8"],
                    "path_hash_size": 2,
                    "hops": 2,
                },
                "observer_id": self.OTHER,
                "observer_name": "Hilltop obs",
                "snr": 6.25,
                "rssi": -97.0,
                "source_node": {"public_key": "CD" * 32, "name": "Solar", "lat": 45.5, "lon": -75.5},
            },
        )

    def test_unreported_signal_and_unsure_source_are_left_out(self):
        unsure = {"confidence": "ambiguous", "nodes": [{"publicKey": "CD" * 32}, {"publicKey": "CE" * 32}]}
        observation = parse_feed_observation(_feed_data(self.OTHER, snr=0, rssi=0, resolvedSource=unsure), PUBKEY_HEX)
        for key in ("snr", "rssi", "source_node"):
            self.assertNotIn(key, observation)

    def test_trace_path_is_snr_list(self):
        data = _feed_data(self.OTHER, payload_type=9, pathBytes="14f0", pathLength={"hashSize": 1})
        decoded = parse_feed_observation(data, PUBKEY_HEX)["decoded"]
        self.assertEqual((decoded["payload_type_name"], decoded["path"], decoded["trace_snrs"], decoded["hops"]),
                         ("TRACE", [], [5.0, -4.0], 2))

    def test_skips_own_uploads_and_garbage(self):
        self.assertIsNone(parse_feed_observation(_feed_data(PUBKEY_HEX), PUBKEY_HEX))
        self.assertIsNone(parse_feed_observation(_feed_data(self.OTHER, pathBytes="b2be3d"), PUBKEY_HEX))
        self.assertIsNone(parse_feed_observation({**_feed_data(self.OTHER), "packetHash": "xyz"}, PUBKEY_HEX))
        self.assertIsNone(parse_feed_observation(_feed_data(self.OTHER, payload_type=16), PUBKEY_HEX))
        self.assertIsNone(parse_feed_observation({"packet": {}}, PUBKEY_HEX))
        self.assertIsNone(parse_feed_observation(None, PUBKEY_HEX))

    def test_reconnect_backoff(self):
        self.assertEqual([feed_backoff_seconds(attempt, 0.0) for attempt in (0, 1, 3, 4, 5, 9)], [1, 2, 8, 16, 30, 30])
        self.assertEqual((feed_backoff_seconds(0, 1.0), feed_backoff_seconds(0, -1.0)), (1.25, 0.75))


class ConfigTests(unittest.TestCase):
    def test_defaults_target_meshmapper_broker(self):
        config = MeshMapperConfig.from_settings({"iata": "yow"})
        self.assertEqual(config.iata, "YOW")
        self.assertEqual((config.server, config.port, config.transport), ("mqtt.meshmapper.net", 443, "websockets"))
        self.assertTrue(config.tls and config.tls_verify)
        self.assertEqual(config.token_audience, "mqtt.meshmapper.net")
        self.assertIsNone(config.validation_error())

    def test_iata_is_required(self):
        self.assertIsNotNone(MeshMapperConfig.from_settings({}).validation_error())
        self.assertIsNotNone(MeshMapperConfig.from_settings({"iata": "XXX"}).validation_error())

    def test_settings_only_come_from_config(self):
        with mock.patch.dict(os.environ, {"MESHMAPPER_IATA": "sea", "MESHMAPPER_PRIVATE_KEY": "00" * 64}):
            config = MeshMapperConfig.from_settings({"iata": "yow", "private_key": RFC_PRIVATE_KEY.hex()})
        self.assertEqual(config.iata, "YOW")
        self.assertEqual(config.private_key, RFC_PRIVATE_KEY.hex().upper())

    def test_subscriber_settings(self):
        config = MeshMapperConfig.from_settings({"iata": "YOW", "port": 8883, "transport": "tcp"})
        self.assertEqual(
            (config.subscribe_server, config.subscribe_port, config.subscribe_transport, config.subscribe_tls),
            ("mqtt.meshmapper.net", 8883, "tcp", True),
        )
        self.assertIsNotNone(config.subscriber_disabled_reason())
        config = MeshMapperConfig.from_settings(
            {
                "iata": "YOW",
                "subscribe_username": "viewer",
                "subscribe_password": "secret",
                "subscribe_server": "broker.example.org",
                "subscribe_port": 9001,
                "subscribe_tls": False,
            }
        )
        self.assertEqual((config.subscribe_username, config.subscribe_password), ("viewer", "secret"))
        self.assertEqual((config.subscribe_server, config.subscribe_port, config.subscribe_tls), ("broker.example.org", 9001, False))
        self.assertIsNone(config.subscriber_disabled_reason())
        config.subscribe = False
        self.assertIsNotNone(config.subscriber_disabled_reason())

    def test_live_feed_settings(self):
        config = MeshMapperConfig.from_settings({"iata": "YOW"})
        self.assertEqual((config.live_feed, config.live_feed_url), (True, "wss://analyzer.meshmapper.net/ws"))
        self.assertIsNone(config.live_feed_disabled_reason())  # needs no account
        self.assertIsNotNone(MeshMapperConfig.from_settings({"iata": "YOW", "live_feed": False}).live_feed_disabled_reason())
        self.assertIsNotNone(MeshMapperConfig.from_settings({"iata": "YOW", "subscribe": False}).live_feed_disabled_reason())

    def test_invalid_private_key_is_rejected(self):
        self.assertIsNotNone(MeshMapperConfig.from_settings({"iata": "YOW", "private_key": "abcd"}).validation_error())

    def test_plugin_is_registered(self):
        self.assertIs(load_plugin_class("meshmapper"), MeshMapperPlugin)


class _FakePublishInfo:
    def wait_for_publish(self, timeout=None):
        return None


class _FakeMqttClient:
    def __init__(self, client_id: str, transport: str):
        self.client_id = client_id
        self.transport = transport
        self.credentials: list[tuple[str, str]] = []
        self.tls: dict[str, Any] = {}
        self.ws_options: dict[str, Any] = {}
        self.will: tuple | None = None
        self.connect_args: tuple | None = None
        self.published: list[tuple[str, dict, int, bool]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.loop_started = False
        self.disconnected = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_connect_fail = None
        self.on_subscribe = None
        self.on_message = None

    def username_pw_set(self, username, password):
        self.credentials.append((username, password))

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def tls_insecure_set(self, value):
        self.tls["insecure"] = value

    def ws_set_options(self, **kwargs):
        self.ws_options = kwargs

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, json.loads(payload), qos, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))
        return _FakePublishInfo()

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return (0, len(self.subscriptions))


class _FakeMessage:
    def __init__(self, topic: str, payload: dict):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


class _ReasonCode:
    def __init__(self, value: int):
        self.value = value
        self.is_failure = value >= 0x80 or value in {1, 2, 3, 4, 5}

    def __str__(self):
        return f"rc={self.value}"


class _FakeTransport:
    def __init__(self):
        self.is_connected = True
        self.device_self_info = {
            "public_key": RFC_PUBLIC_KEY.hex(),
            "name": "Gateway",
            "radio_freq": 869.525,
            "radio_bw": 250.0,
            "radio_sf": 11,
            "radio_cr": 5,
        }
        self.listeners: list = []
        self.signed: list[bytes] = []
        self.remote: list[dict] = []

    def add_rx_log_listener(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_rx_log_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def query_device_info(self):
        return {"model": "Heltec V3", "ver": "1.9.1", "fw_build": "01-Mar-2026"}

    async def sign_with_device(self, data: bytes) -> bytes:
        self.signed.append(data)
        return sign_with_expanded_key(data, RFC_PRIVATE_KEY, RFC_PUBLIC_KEY)

    async def dispatch_remote_rx_log(self, rx_log: dict) -> None:
        self.remote.append(rx_log)


class _FakeFeedSocket:
    def __init__(self, url: str):
        self.url = url
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.close_code = None

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True

    async def deliver(self, message):
        """Hand the feed client a message (or an exception for ``recv`` to raise) and let it run."""
        self.incoming.put_nowait(message if isinstance(message, BaseException) else json.dumps(message))
        for _ in range(10):
            await asyncio.sleep(0)

    async def open_subscription(self):
        await self.deliver({"v": 1, "type": "hello", "connectionId": "c1"})
        await self.deliver({"v": 1, "type": "subscribed", "id": "subscribe", "subscriptionId": "s1"})


def _feed_event(observer_key: str) -> dict:
    return {"v": 1, "type": "event", "event": "packetObservation", "data": _feed_data(observer_key)}


def _make_context() -> PluginContext:
    settings = MeshgramSettings(
        telegram_bot_token="token",
        telegram_group_id=-100,
        config_path="config.yaml",
        plugins=[],
    )
    return PluginContext(settings=settings, telegram_group_id=-100, mesh_payload_limit=140, local_node_id=None)


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.clients: list[_FakeMqttClient] = []

        def factory(client_id, transport):
            client = _FakeMqttClient(client_id, transport)
            self.clients.append(client)
            return client

        self.feed_sockets: list[_FakeFeedSocket] = []

        async def feed_connect(url):
            socket = _FakeFeedSocket(url)
            self.feed_sockets.append(socket)
            return socket

        self.plugin = MeshMapperPlugin({"iata": "yow"})
        self.plugin.uploader = MeshMapperUploader(self.plugin.config, client_factory=factory)
        self.plugin.subscriber = MeshMapperSubscriber(self.plugin.config, client_factory=factory)
        self.plugin.live_feed = MeshMapperLiveFeed(self.plugin.config, connect=feed_connect)
        self.transport = _FakeTransport()

    async def _feed_socket(self, count: int = 1) -> _FakeFeedSocket:
        for _ in range(20):
            if len(self.feed_sockets) >= count:
                break
            await asyncio.sleep(0)
        return self.feed_sockets[count - 1]

    async def _connect(self) -> _FakeMqttClient:
        await self.plugin.on_mesh_connected(self.transport, _make_context())
        for _ in range(20):
            if self.clients:
                break
            await asyncio.sleep(0)
        client = self.clients[0]
        client.on_connect(client, None, None, _ReasonCode(0), None)
        return client

    def test_connects_with_device_signed_token_and_publishes(self):
        async def scenario():
            client = await self._connect()
            topic_base = f"meshcore/YOW/{PUBKEY_HEX}"

            # MQTT session matches MeshMapper's broker requirements.
            self.assertEqual(client.transport, "websockets")
            self.assertEqual(client.connect_args, ("mqtt.meshmapper.net", 443, 60))
            self.assertEqual(client.ws_options, {"path": "/"})
            self.assertFalse(client.tls["insecure"])
            self.assertEqual(client.client_id, f"meshgram_{PUBKEY_HEX}"[:23])

            username, token = client.credentials[0]
            self.assertEqual(username, f"v1_{PUBKEY_HEX}")
            header, payload, signature = token.split(".")
            self.assertEqual(_b64url_json(header), {"alg": "Ed25519", "typ": "JWT"})
            claims = _b64url_json(payload)
            self.assertEqual(claims["publicKey"], PUBKEY_HEX)
            self.assertEqual(claims["aud"], "mqtt.meshmapper.net")
            self.assertEqual(claims["client"], CLIENT_VERSION)
            self.assertEqual(claims["exp"] - claims["iat"], 3600)
            self.assertEqual(self.transport.signed, [f"{header}.{payload}".encode()])
            self.assertEqual(
                bytes.fromhex(signature),
                sign_with_expanded_key(f"{header}.{payload}".encode(), RFC_PRIVATE_KEY, RFC_PUBLIC_KEY),
            )

            # Offline LWT + retained online status.
            self.assertEqual(client.will[0], f"{topic_base}/status")
            self.assertEqual(client.will[1]["status"], "offline")
            self.assertTrue(client.will[3])
            topic, status, _, retain = client.published[0]
            self.assertEqual(topic, f"{topic_base}/status")
            self.assertTrue(retain)
            self.assertEqual(status["status"], "online")
            self.assertEqual(status["origin"], "Gateway")
            self.assertEqual(status["origin_id"], PUBKEY_HEX)
            self.assertEqual(status["model"], "Heltec V3")
            self.assertEqual(status["firmware_version"], "v1.9.1 (Build: 01-Mar-2026)")
            self.assertEqual(status["radio"], "869.525,250.0,11,5")

            # RX log → packets topic.
            await self.transport.listeners[0]({"payload": "1500AABB", "snr": 5.5, "rssi": -101})
            topic, packet, _, retain = client.published[-1]
            self.assertEqual(topic, f"{topic_base}/packets")
            self.assertFalse(retain)
            self.assertEqual(packet["raw"], "1500AABB")
            self.assertEqual(packet["origin_id"], PUBKEY_HEX)

            await self.plugin.on_shutdown()
            topic, status, _, retain = client.published[-1]
            self.assertEqual((topic, status["status"], retain), (f"{topic_base}/status", "offline", True))
            self.assertTrue(client.disconnected)
            self.assertFalse(client.loop_started)

        asyncio.run(scenario())

    def _enable_subscriber(self) -> None:
        self.plugin.config.subscribe_username = "viewer"
        self.plugin.config.subscribe_password = "secret"

    def _subscriber_client(self) -> _FakeMqttClient:
        return next(client for client in self.clients if "sub_" in client.client_id)

    def _uploader_client(self) -> _FakeMqttClient:
        return next(client for client in self.clients if "sub_" not in client.client_id)

    def test_device_signed_session_never_subscribes(self):
        # MeshMapper's broker disconnects device-authenticated clients that subscribe.
        async def scenario():
            self._enable_subscriber()
            await self._connect_all()
            self.assertEqual(self._uploader_client().subscriptions, [])
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    async def _connect_all(self) -> None:
        await self.plugin.on_mesh_connected(self.transport, _make_context())
        for _ in range(20):
            if len(self.clients) == 2:
                break
            await asyncio.sleep(0)
        for client in self.clients:
            client.on_connect(client, None, None, _ReasonCode(0), None)

    def test_subscriber_account_receives_other_observers(self):
        async def scenario():
            self._enable_subscriber()
            self.plugin.config.subscribe_server = "regional.example.org"
            self.plugin.config.subscribe_port = 8883
            await self._connect_all()
            client = self._subscriber_client()
            self.assertEqual(client.credentials, [("viewer", "secret")])
            self.assertEqual(client.connect_args, ("regional.example.org", 8883, 60))
            self.assertEqual(client.client_id, f"meshgram_sub_{PUBKEY_HEX}"[:23])
            topic = "meshcore/YOW/+/packets"
            self.assertEqual(client.subscriptions, [(topic, 0)])
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                client.on_subscribe(client, None, 1, [_ReasonCode(0)], None)
            self.assertTrue(self.plugin.subscriber.is_subscribed)

            other = "AB" * 32
            packet = {"origin": "Hilltop obs", "type": "PACKET", "direction": "rx", "raw": "1500AABB", "SNR": "4"}
            client.on_message(client, None, _FakeMessage(f"meshcore/YOW/{other}/packets", packet))
            client.on_message(client, None, _FakeMessage(f"meshcore/YOW/{PUBKEY_HEX}/packets", packet))  # ours
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(
                self.transport.remote,
                [{"payload": "1500AABB", "observer_id": other, "observer_name": "Hilltop obs", "snr": 4.0}],
            )

            # A radio reconnect keeps the existing subscriber session.
            await self.plugin.on_mesh_connected(self.transport, _make_context())
            self.assertEqual(len(self.clients), 2)

            await self.plugin.on_shutdown()
            self.assertTrue(client.disconnected)
            self.assertFalse(client.loop_started)

        asyncio.run(scenario())

    def test_subscriber_defaults_to_upload_broker(self):
        async def scenario():
            self._enable_subscriber()
            await self._connect_all()
            client = self._subscriber_client()
            self.assertEqual(client.connect_args, ("mqtt.meshmapper.net", 443, 60))
            self.assertEqual(client.transport, "websockets")
            self.assertEqual(client.ws_options, {"path": "/"})
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_refused_subscription_is_not_retried(self):
        async def scenario():
            self._enable_subscriber()
            await self._connect_all()
            client = self._subscriber_client()
            with self.assertLogs("meshgram.plugins.meshmapper", level="WARNING"):
                client.on_subscribe(client, None, 1, [_ReasonCode(0x80)], None)
            client.on_connect(client, None, None, _ReasonCode(0), None)  # reconnect
            self.assertEqual(len(client.subscriptions), 1)
            self.assertEqual(self._uploader_client().published[-1][1]["status"], "online")  # uploads carry on
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_no_subscriber_without_credentials(self):
        # Other observers' packets then come from the live feed alone.
        async def scenario():
            await self._connect()
            await self._feed_socket()
            self.assertEqual(len(self.clients), 1)
            self.assertEqual(self.clients[0].subscriptions, [])
            self.assertEqual(len(self.feed_sockets), 1)
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_explains_when_nothing_receives_other_observers(self):
        async def scenario():
            self.plugin.config.live_feed = False
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO") as logs:
                await self._connect()
            self.assertTrue(any("subscribe_username" in line for line in logs.output))
            self.assertEqual(self.feed_sockets, [])
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_subscribing_can_be_turned_off(self):
        async def scenario():
            self._enable_subscriber()
            self.plugin.config.subscribe = False
            client = await self._connect()
            self.assertEqual(len(self.clients), 1)
            self.assertEqual(client.subscriptions, [])
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(self.feed_sockets, [])
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_live_feed_receives_other_observers_without_an_account(self):
        async def scenario():
            status = StatusRegistry()
            context = _make_context()
            context.status = status
            await self.plugin.on_startup(context)
            self.assertEqual(status.get("meshmapper_feed")["state"], "connecting")

            await self.plugin.on_mesh_connected(self.transport, context)
            socket = await self._feed_socket()
            self.assertEqual(socket.url, "wss://analyzer.meshmapper.net/ws")
            await socket.deliver({"v": 1, "type": "hello", "connectionId": "c1"})
            configure, subscribe = socket.sent
            self.assertEqual(
                {key: configure[key] for key in ("type", "resolvePath", "includeObserverKey", "includeRepeats")},
                {"type": "configure", "resolvePath": False, "includeObserverKey": True, "includeRepeats": True},
            )
            self.assertEqual((subscribe["type"], subscribe["scope"]), ("subscribe", {"events": ["packetObservation"], "iatas": ["YOW"]}))
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                await socket.deliver({"v": 1, "type": "subscribed", "id": "subscribe", "subscriptionId": "s1"})
            self.assertEqual(status.get("meshmapper_feed")["state"], "connected")
            self.assertTrue(self.plugin.live_feed.is_subscribed)

            other = "AB" * 32
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                await socket.deliver(_feed_event(other))
            await socket.deliver(_feed_event(PUBKEY_HEX))  # our own upload
            await socket.deliver({"v": 1, "type": "pong", "id": "ping-1"})
            self.assertEqual([rx["observer_id"] for rx in self.transport.remote], [other])
            self.assertEqual(self.transport.remote[0]["decoded"]["hash"], "73408C45E49BD15F")

            # A radio reconnect keeps the feed session.
            await self.plugin.on_mesh_connected(self.transport, context)
            await asyncio.sleep(0)
            self.assertEqual(len(self.feed_sockets), 1)

            await self.plugin.on_shutdown()
            self.assertTrue(socket.closed)
            # The plugin is off: its indicators go away.
            self.assertIsNone(status.get("meshmapper_feed"))

        asyncio.run(scenario())

    def test_live_feed_stands_by_while_mqtt_subscription_is_up(self):
        # The MQTT subscription carries the full packets, so it wins; no duplicates.
        async def scenario():
            self._enable_subscriber()
            await self._connect_all()
            client = self._subscriber_client()
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                client.on_subscribe(client, None, 1, [_ReasonCode(0)], None)
            socket = await self._feed_socket()
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                await socket.open_subscription()
            other = "AB" * 32
            await socket.deliver(_feed_event(other))
            self.assertEqual(self.transport.remote, [])

            with self.assertLogs("meshgram.plugins.meshmapper", level="WARNING"):
                client.on_disconnect(client, None, None, _ReasonCode(7), None)
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                await socket.deliver(_feed_event(other))
            self.assertEqual([rx["observer_id"] for rx in self.transport.remote], [other])
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_live_feed_reconnects_with_backoff(self):
        async def scenario():
            attempts: list[int] = []
            failures = [OSError("unreachable")]
            fake_connect = self.plugin.live_feed._connect

            async def flaky_connect(url):
                if failures:
                    raise failures.pop()
                return await fake_connect(url)

            self.plugin.live_feed._connect = flaky_connect
            backoff = mock.patch(
                "meshgram.plugins.meshmapper.feed_backoff_seconds",
                side_effect=lambda attempt, jitter: attempts.append(attempt) or 0,
            )
            with backoff, self.assertLogs("meshgram.plugins.meshmapper", level="WARNING") as logs:
                await self._connect()
                socket = await self._feed_socket()
            self.assertIn("unreachable", logs.output[0])
            self.assertEqual(attempts, [0])

            with backoff:
                with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                    await socket.open_subscription()
                # The feed sheds load: the next attempt waits the longest.
                socket.close_code = 1013
                with self.assertLogs("meshgram.plugins.meshmapper", level="WARNING"):
                    await socket.deliver(ConnectionError("going away"))
                await self._feed_socket(2)
            self.assertTrue(socket.closed)
            self.assertEqual(attempts, [0, 5])
            self.assertEqual(len(self.feed_sockets), 2)
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_reports_connection_status(self):
        async def scenario():
            self._enable_subscriber()
            status = StatusRegistry()
            context = _make_context()
            context.status = status
            await self.plugin.on_startup(context)
            self.assertEqual(status.get("mqtt_publish")["state"], "connecting")
            self.assertEqual(status.get("mqtt_subscribe")["state"], "connecting")

            await self.plugin.on_mesh_connected(self.transport, context)
            for _ in range(20):
                if len(self.clients) == 2:
                    break
                await asyncio.sleep(0)
            uploader, subscriber = self._uploader_client(), self._subscriber_client()
            uploader.on_connect(uploader, None, None, _ReasonCode(0), None)
            self.assertEqual(status.get("mqtt_publish")["state"], "connected")
            subscriber.on_connect(subscriber, None, None, _ReasonCode(0), None)
            self.assertEqual(status.get("mqtt_subscribe")["state"], "connecting")
            subscriber.on_subscribe(subscriber, None, 1, [_ReasonCode(0)], None)
            self.assertEqual(status.get("mqtt_subscribe")["state"], "connected")

            with self.assertLogs("meshgram.plugins.meshmapper", level="WARNING"):
                uploader.on_disconnect(uploader, None, None, _ReasonCode(7), None)
            self.assertEqual(status.get("mqtt_publish")["state"], "disconnected")
            uploader.on_connect(uploader, None, None, _ReasonCode(0), None)
            uploader.on_connect_fail(uploader, None)
            self.assertEqual(status.get("mqtt_publish")["state"], "disconnected")
            self.assertIn("Can't reach", status.get("mqtt_publish")["detail"])
            with self.assertLogs("meshgram.plugins.meshmapper", level="ERROR"):
                subscriber.on_connect(subscriber, None, None, _ReasonCode(135), None)
            self.assertEqual(status.get("mqtt_subscribe")["state"], "disconnected")
            self.assertIn("credentials", status.get("mqtt_subscribe")["detail"])
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_status_without_subscriber_credentials_is_disabled(self):
        async def scenario():
            status = StatusRegistry()
            context = _make_context()
            context.status = status
            await self.plugin.on_startup(context)
            entry = status.get("mqtt_subscribe")
            self.assertEqual(entry["state"], "disabled")
            self.assertIn("subscriber account", entry["detail"])
            self.assertEqual(status.get("meshmapper_feed")["state"], "connecting")

        asyncio.run(scenario())

    def test_status_disabled_when_misconfigured(self):
        async def scenario():
            status = StatusRegistry()
            context = _make_context()
            context.status = status
            with self.assertLogs("meshgram.plugins.meshmapper", level="ERROR"):
                await MeshMapperPlugin({}).on_startup(context)
            for key in ("mqtt_publish", "mqtt_subscribe", "meshmapper_feed"):
                self.assertEqual(status.get(key)["state"], "disabled")

        asyncio.run(scenario())

    def test_shutdown_releases_the_transport_and_status(self):
        async def scenario():
            status = StatusRegistry()
            context = _make_context()
            context.status = status
            await self.plugin.on_startup(context)
            await self.plugin.on_mesh_connected(self.transport, context)
            self.assertEqual(len(self.transport.listeners), 1)
            await self.plugin.on_shutdown()
            # Turned off at runtime: no stale listener or connection indicators.
            self.assertEqual(self.transport.listeners, [])
            self.assertEqual(status.snapshot(), [])

        asyncio.run(scenario())

    def test_packets_are_not_published_until_broker_connects(self):
        async def scenario():
            await self.plugin.on_mesh_connected(self.transport, _make_context())
            for _ in range(20):
                await asyncio.sleep(0)
            await self.transport.listeners[0]({"payload": "1500AABB"})
            self.assertEqual(self.clients[0].published, [])
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_reconnect_reuses_session_and_listener(self):
        async def scenario():
            client = await self._connect()
            await self.plugin.on_mesh_connected(self.transport, _make_context())
            self.assertEqual(len(self.clients), 1)
            self.assertEqual(len(self.transport.listeners), 1)
            self.assertEqual(client.published[-1][1]["status"], "online")
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_auth_failure_triggers_token_refresh(self):
        async def scenario():
            client = await self._connect()
            with self.assertLogs("meshgram.plugins.meshmapper", level="ERROR"):
                client.on_connect(client, None, None, _ReasonCode(135), None)
            for _ in range(20):
                if len(client.credentials) > 1:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(client.credentials), 2)
            self.assertFalse(self.plugin.uploader.is_connected)
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_configured_private_key_signs_locally(self):
        async def scenario():
            self.plugin.config.private_key = RFC_PRIVATE_KEY.hex().upper()
            client = await self._connect()
            self.assertEqual(self.transport.signed, [])
            _, token = client.credentials[0]
            header, payload, signature = token.split(".")
            self.assertEqual(
                bytes.fromhex(signature),
                sign_with_expanded_key(f"{header}.{payload}".encode(), RFC_PRIVATE_KEY, RFC_PUBLIC_KEY),
            )
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_mismatched_private_key_falls_back_to_device_signing(self):
        async def scenario():
            self.plugin.config.private_key = _expanded_key(b"\x01" * 32).hex().upper()
            with self.assertLogs("meshgram.plugins.meshmapper", level="ERROR"):
                await self._connect()
            self.assertEqual(len(self.transport.signed), 1)
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_disabled_without_iata(self):
        async def scenario():
            plugin = MeshMapperPlugin({})
            with self.assertLogs("meshgram.plugins.meshmapper", level="ERROR"):
                await plugin.on_mesh_connected(self.transport, _make_context())
            self.assertEqual(self.transport.listeners, [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

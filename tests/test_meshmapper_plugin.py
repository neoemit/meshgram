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
from meshgram.config import MESHCORE_BACKEND, MESHTASTIC_BACKEND, MeshgramSettings, PluginConfig
from meshgram.plugin import load_plugins
from meshgram.plugins.meshmapper import (
    CLIENT_VERSION,
    MeshMapperConfig,
    MeshMapperPlugin,
    MeshMapperUploader,
    build_packet_message,
    packet_hash,
    parse_observer_packet,
)
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

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"MESHMAPPER_IATA": "sea", "MESHMAPPER_PRIVATE_KEY": RFC_PRIVATE_KEY.hex()}):
            config = MeshMapperConfig.from_settings({"iata": "YOW"})
        self.assertEqual(config.iata, "SEA")
        self.assertEqual(config.private_key, RFC_PRIVATE_KEY.hex().upper())

    def test_invalid_private_key_is_rejected(self):
        self.assertIsNotNone(MeshMapperConfig.from_settings({"iata": "YOW", "private_key": "abcd"}).validation_error())

    def test_plugin_is_registered(self):
        plugins = load_plugins([PluginConfig(name="meshmapper", enabled=True, settings={"iata": "YOW"})])
        self.assertIsInstance(plugins[0].instance, MeshMapperPlugin)


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

    async def query_device_info(self):
        return {"model": "Heltec V3", "ver": "1.9.1", "fw_build": "01-Mar-2026"}

    async def sign_with_device(self, data: bytes) -> bytes:
        self.signed.append(data)
        return sign_with_expanded_key(data, RFC_PRIVATE_KEY, RFC_PUBLIC_KEY)

    async def dispatch_remote_rx_log(self, rx_log: dict) -> None:
        self.remote.append(rx_log)


def _make_context(backend=MESHCORE_BACKEND) -> PluginContext:
    settings = MeshgramSettings(
        telegram_bot_token="token",
        telegram_group_id=-100,
        config_path="config.yaml",
        plugins=[],
    )
    settings.mesh.backend = backend
    return PluginContext(settings=settings, telegram_group_id=-100, mesh_payload_limit=140, local_node_id=None)


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.clients: list[_FakeMqttClient] = []

        def factory(client_id, transport):
            client = _FakeMqttClient(client_id, transport)
            self.clients.append(client)
            return client

        self.plugin = MeshMapperPlugin({"iata": "yow"})
        self.plugin.uploader = MeshMapperUploader(self.plugin.config, client_factory=factory)
        self.transport = _FakeTransport()

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

    def test_subscribes_to_region_and_forwards_other_observers(self):
        async def scenario():
            client = await self._connect()
            topic = "meshcore/YOW/+/packets"
            self.assertEqual(client.subscriptions, [(topic, 0)])
            with self.assertLogs("meshgram.plugins.meshmapper", level="INFO"):
                client.on_subscribe(client, None, 1, [_ReasonCode(0)], None)

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
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_refused_subscription_is_not_retried(self):
        async def scenario():
            client = await self._connect()
            with self.assertLogs("meshgram.plugins.meshmapper", level="WARNING") as logs:
                client.on_subscribe(client, None, 1, [_ReasonCode(0x80)], None)
            self.assertIn("Uploads are unaffected", logs.output[0])
            client.on_connect(client, None, None, _ReasonCode(0), None)  # reconnect
            self.assertEqual(len(client.subscriptions), 1)
            self.assertEqual(client.published[-1][1]["status"], "online")  # uploads carry on
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_disconnect_right_after_subscribing_stops_subscribing(self):
        async def scenario():
            client = await self._connect()
            with self.assertLogs("meshgram.plugins.meshmapper", level="WARNING"):
                client.on_disconnect(client, None, None, _ReasonCode(135), None)
            client.on_connect(client, None, None, _ReasonCode(0), None)
            self.assertEqual(len(client.subscriptions), 1)
            await self.plugin.on_shutdown()

        asyncio.run(scenario())

    def test_subscribing_can_be_turned_off(self):
        async def scenario():
            self.plugin.config.subscribe = False
            client = await self._connect()
            self.assertEqual(client.subscriptions, [])
            await self.plugin.on_shutdown()

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

    def test_disabled_on_meshtastic_backend(self):
        async def scenario():
            with self.assertLogs("meshgram.plugins.meshmapper", level="ERROR"):
                await self.plugin.on_mesh_connected(self.transport, _make_context(MESHTASTIC_BACKEND))
            self.assertEqual(self.transport.listeners, [])
            self.assertEqual(self.clients, [])

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

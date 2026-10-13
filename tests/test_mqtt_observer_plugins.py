import asyncio
import base64
import json
import unittest

from meshgram.plugin import load_plugin_class
from meshgram.plugins.letsmesh import LetsMeshPlugin
from meshgram.plugins.meshrank import MeshRankPlugin
from meshgram.plugins.mqtt_observer import ObserverSubscriber, ObserverUploader
from meshgram.settings_schema import SettingsError, validate
from meshgram.status import StatusRegistry

try:
    from test_meshmapper_plugin import PUBKEY_HEX, _FakeMessage, _FakeMqttClient, _FakeTransport, _make_context, _ReasonCode
except ImportError:
    from tests.test_meshmapper_plugin import (
        PUBKEY_HEX,
        _FakeMessage,
        _FakeMqttClient,
        _FakeTransport,
        _make_context,
        _ReasonCode,
    )


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class ObserverPluginTestCase(unittest.TestCase):
    plugin_class = None

    def make(self, settings: dict):
        self.clients: list[_FakeMqttClient] = []

        def factory(client_id, transport):
            client = _FakeMqttClient(client_id, transport)
            self.clients.append(client)
            return client

        plugin = self.plugin_class(settings)
        plugin.uploaders = [ObserverUploader(config, client_factory=factory) for config in plugin.configs]
        plugin.subscriber = ObserverSubscriber(plugin.configs[0], client_factory=factory) if plugin.configs else None
        self.transport = _FakeTransport()
        self.status = StatusRegistry()
        self.context = _make_context()
        self.context.status = self.status
        return plugin

    async def connect(self, plugin, count: int) -> list[_FakeMqttClient]:
        await plugin.on_startup(self.context)
        await plugin.on_mesh_connected(self.transport, self.context)
        for _ in range(20):
            if len(self.clients) >= count:
                break
            await asyncio.sleep(0)
        for client in self.clients:
            client.on_connect(client, None, None, _ReasonCode(0), None)
        return self.clients


class LetsMeshTests(ObserverPluginTestCase):
    plugin_class = LetsMeshPlugin

    def test_registered(self):
        self.assertIs(load_plugin_class("letsmesh"), LetsMeshPlugin)

    def test_settings(self):
        validate({"iata": "YOW", "servers": ["eu"]}, LetsMeshPlugin.settings_schema)
        with self.assertRaises(SettingsError):
            validate({"iata": "YOW", "servers": ["asia"]}, LetsMeshPlugin.settings_schema)
        with self.assertRaises(SettingsError):
            validate({"iata": "YOW", "servers": []}, LetsMeshPlugin.settings_schema)
        self.assertEqual([c.server for c in LetsMeshPlugin({"iata": "yow", "servers": ["EU"]}).configs], ["mqtt-eu-v1.letsmesh.net"])

    def test_uploads_to_both_servers_with_device_signed_tokens(self):
        plugin = self.make({"iata": "yow"})

        async def scenario():
            clients = await self.connect(plugin, 2)
            self.assertEqual(
                [(c.connect_args, c.transport, c.ws_options) for c in clients],
                [
                    (("mqtt-us-v1.letsmesh.net", 443, 60), "websockets", {"path": "/"}),
                    (("mqtt-eu-v1.letsmesh.net", 443, 60), "websockets", {"path": "/"}),
                ],
            )
            for client, audience in zip(clients, ("mqtt-us-v1.letsmesh.net", "mqtt-eu-v1.letsmesh.net")):
                username, token = client.credentials[0]
                self.assertEqual(username, f"v1_{PUBKEY_HEX}")
                self.assertEqual((_claims(token)["aud"], _claims(token)["publicKey"]), (audience, PUBKEY_HEX))
                self.assertEqual(client.published[0][0], f"meshcore/YOW/{PUBKEY_HEX}/status")
                self.assertTrue(client.published[0][3])  # retained
            self.assertEqual(self.status.get("letsmesh_us_publish")["state"], "connected")
            self.assertEqual(self.status.get("letsmesh_eu_publish")["label"], "LetsMesh EU MQTT (publish)")
            self.assertEqual(self.status.get("letsmesh_subscribe")["state"], "disabled")  # no account

            await self.transport.listeners[0]({"payload": "1500AABB"})
            await self.transport.listeners[1]({"payload": "1500AABB"})
            self.assertEqual([c.published[-1][0] for c in clients], [f"meshcore/YOW/{PUBKEY_HEX}/packets"] * 2)

            await plugin.on_shutdown()
            self.assertEqual(self.transport.listeners, [])
            self.assertTrue(all(c.disconnected for c in clients))
            self.assertEqual(self.status.snapshot(), [])

        asyncio.run(scenario())

    def test_subscriber_account_stamps_packets_with_the_network(self):
        plugin = self.make({"iata": "yow", "servers": ["eu"], "subscribe_username": "viewer", "subscribe_password": "pw"})

        async def scenario():
            await self.connect(plugin, 2)
            subscriber = next(c for c in self.clients if "sub_" in c.client_id)
            self.assertEqual(subscriber.connect_args, ("mqtt-eu-v1.letsmesh.net", 443, 60))
            self.assertEqual(subscriber.credentials, [("viewer", "pw")])
            self.assertEqual(subscriber.subscriptions, [("meshcore/YOW/+/packets", 0)])
            subscriber.on_subscribe(subscriber, None, 1, [_ReasonCode(0)], None)
            other = "AB" * 32
            subscriber.on_message(subscriber, None, _FakeMessage(f"meshcore/YOW/{other}/packets", {"raw": "1500AABB", "origin": "Hill"}))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(self.transport.remote[0]["network"], "LetsMesh")
            await plugin.on_shutdown()

        asyncio.run(scenario())

    def test_missing_region_disables_it(self):
        plugin = self.make({})

        async def scenario():
            await plugin.on_startup(self.context)
            await plugin.on_mesh_connected(self.transport, self.context)
            self.assertEqual(self.clients, [])
            self.assertEqual(self.status.get("letsmesh_us_publish")["state"], "disabled")
            self.assertIn("letsmesh", self.status.get("letsmesh_us_publish")["detail"])

        with self.assertLogs("meshgram.plugins", level="ERROR"):
            asyncio.run(scenario())


class MeshRankTests(ObserverPluginTestCase):
    plugin_class = MeshRankPlugin

    def test_registered(self):
        self.assertIs(load_plugin_class("meshrank"), MeshRankPlugin)

    def test_uploads_anonymously_over_tls(self):
        plugin = self.make({"iata": "yow"})

        async def scenario():
            (client,) = await self.connect(plugin, 1)
            self.assertEqual((client.connect_args, client.transport), (("meshrank.net", 8883, 60), "tcp"))
            self.assertEqual(client.credentials, [])  # known by the public key in its topics
            self.assertEqual(client.ws_options, {})
            self.assertFalse(client.tls["insecure"])
            self.assertEqual(self.transport.signed, [])  # no token to sign
            # MeshRank doesn't retain status documents.
            self.assertFalse(client.will[3])
            self.assertEqual((client.published[0][0], client.published[0][3]), (f"meshcore/YOW/{PUBKEY_HEX}/status", False))
            self.assertEqual(self.status.get("meshrank_publish")["state"], "connected")

            # A radio reconnect keeps the one session.
            await plugin.on_mesh_connected(self.transport, self.context)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(len(self.clients), 1)
            await plugin.on_shutdown()

        asyncio.run(scenario())

    def test_registration_login(self):
        plugin = self.make({"iata": "yow", "username": "observer-1", "password": "secret"})

        async def scenario():
            (client,) = await self.connect(plugin, 1)
            self.assertEqual(client.credentials, [("observer-1", "secret")])
            await plugin.on_shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

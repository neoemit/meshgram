import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from meshgram.config import MeshgramSettings, PluginConfig, WebConfig
from meshgram.plugin_manager import PluginManager, PluginOverrideStore
from meshgram.radio_admin import RadioAdmin, hashtag_secret
from meshgram.status import StatusRegistry
from meshgram.web import WebServer
from meshgram.web.api import channel_usage, message_max_bytes, register_control_api

try:
    from test_radio_admin import FakeRadio  # unittest discover -s tests
except ImportError:
    from tests.test_radio_admin import FakeRadio  # python -m unittest tests.test_web_server


async def http(port, method, path, body=None, headers=None, raw_body=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    headers = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
    if body is not None and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    if data:
        headers["Content-Length"] = str(len(data))
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    writer.write(head.encode() + data)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, payload = response.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split(" ")[1])
    response_headers = {name.lower(): value for name, _, value in (line.partition(": ") for line in lines[1:])}
    return status, response_headers, payload


def as_json(payload):
    return json.loads(payload) if payload else None


class WebServerTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, **config) -> WebServer:
        server = WebServer(WebConfig(**{"port": 0, **config}), StatusRegistry())
        await server.start()
        self.addAsyncCleanup(server.stop)
        return server

    async def test_serves_page_assets_and_state(self):
        server = await self._start(title="My </script> map")
        status, headers, body = await http(server.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"leaflet", body)
        self.assertIn(b"My <\\/script> map", body)
        self.assertNotIn(b"__MESHGRAM_CONFIG__", body)
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")

        status, headers, body = await http(server.port, "GET", "/control.js")
        self.assertEqual((status, headers["content-type"]), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"MeshgramControl", body)

        status, _, _ = await http(server.port, "GET", "/nope")
        self.assertEqual(status, 404)
        status, _, body = await http(server.port, "GET", "/healthz")
        self.assertEqual((status, body), (200, b"ok"))

    async def test_password_protection(self):
        server = await self._start(password="s3cret")
        status, headers, _ = await http(server.port, "GET", "/")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["www-authenticate"])
        token = base64.b64encode(b"anyone:s3cret").decode()
        status, _, _ = await http(server.port, "GET", "/", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(status, 200)
        token = base64.b64encode(b"anyone:wrong").decode()
        status, _, _ = await http(server.port, "GET", "/", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(status, 401)

    async def test_routes_params_and_errors(self):
        server = await self._start()
        seen = []

        async def handler(request):
            seen.append((request.params, request.query, request.json()))
            return SimpleNamespace(status=201, body=b"{}", content_type="application/json", headers={})

        server.route("POST", "/api/things/{name}", handler)
        status, _, _ = await http(server.port, "POST", "/api/things/a%20b?x=1", body={"k": 1})
        self.assertEqual(status, 201)
        self.assertEqual(seen, [({"name": "a b"}, {"x": "1"}, {"k": 1})])

        status, _, body = await http(server.port, "GET", "/api/things/a")
        self.assertEqual((status, as_json(body)["error"]), (405, "Method not allowed"))
        status, _, body = await http(server.port, "POST", "/api/things/a", raw_body=b"{nope", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        # Refused from the headers alone, before the body is read.
        status, _, _ = await http(server.port, "POST", "/api/things/a", raw_body=b"", headers={"Content-Type": "application/json", "Content-Length": "300000"})
        self.assertEqual(status, 413)

    async def test_changes_must_come_from_the_page(self):
        server = await self._start()

        async def handler(request):
            return SimpleNamespace(status=204, body=b"", content_type="text/plain", headers={})

        server.route("POST", "/api/act", handler)
        server.route("DELETE", "/api/act", handler)
        port = server.port
        same = {"Origin": f"http://127.0.0.1:{port}"}
        self.assertEqual((await http(port, "POST", "/api/act", body={}, headers=same))[0], 204)
        # A form posted from another site: wrong origin, or not JSON.
        self.assertEqual((await http(port, "POST", "/api/act", body={}, headers={"Origin": "https://evil.example"}))[0], 403)
        self.assertEqual((await http(port, "POST", "/api/act", body={}, headers={"Sec-Fetch-Site": "cross-site"}))[0], 403)
        status, _, _ = await http(port, "POST", "/api/act", raw_body=b"a=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        self.assertEqual((await http(port, "DELETE", "/api/act", headers=same))[0], 204)

    async def test_read_only_without_password_on_the_network(self):
        server = await self._start(host="0.0.0.0")

        async def handler(request):
            return SimpleNamespace(status=204, body=b"", content_type="text/plain", headers={})

        server.route("POST", "/api/act", handler)
        status, _, body = await http(server.port, "POST", "/api/act", body={})
        self.assertEqual(status, 403)
        self.assertIn("web.password", as_json(body)["error"])
        self.assertFalse(server.events.snapshot()["control"]["allows_changes"])

    async def test_event_stream_sends_snapshot_then_events(self):
        server = await self._start()
        server.status.set_state("radio", "connected", "meshcore serial", label="Radio")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()

        async def next_event() -> dict:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if line.startswith(b"data: "):
                    return json.loads(line[6:])

        snapshot = await next_event()
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual([c["key"] for c in snapshot["connections"]], ["radio"])
        self.assertTrue(snapshot["control"]["allows_changes"])

        server.status.set_state("mqtt_publish", "disconnected", "refused", label="MQTT")
        event = await next_event()
        self.assertEqual((event["type"], event["connection"]["key"]), ("connection", "mqtt_publish"))

        server.events.add_snapshot_provider("map", lambda: {"map": {"nodes": []}})
        server.events.resync()
        self.assertEqual((await next_event())["map"], {"nodes": []})
        writer.close()


class _Host:
    def __init__(self, radio):
        self.radio = radio

    def plugin_context(self):
        return SimpleNamespace(web=None, status=None)

    async def execute_actions(self, actions, plugin_name):
        pass

    def connected_transport(self):
        return None


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": tempdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.settings = MeshgramSettings(
            telegram_bot_token="t",
            telegram_group_id=-1,
            config_path="config.yaml",
            plugins=[PluginConfig("bridge"), PluginConfig("ping_pong", settings={"channels": [1]})],
        )
        self.radio = FakeRadio()
        self.sent = []
        self.plugins = PluginManager(self.settings.plugins, _Host(self.radio), PluginOverrideStore(Path(tempdir.name) / "plugins.json"))
        await self.plugins.start_all()
        self.addAsyncCleanup(self.plugins.stop_all)

        async def send_mesh(action):
            self.sent.append(action)
            return "mc-out-1"

        self.web = WebServer(WebConfig(port=0), StatusRegistry())
        register_control_api(self.web, settings=self.settings, admin=RadioAdmin(self.radio), plugins=self.plugins, send_mesh=send_mesh)
        await self.web.start()
        self.addAsyncCleanup(self.web.stop)
        self.events = self.web.events.subscribe()

    async def call(self, method, path, body=None):
        if body is None and method in {"POST", "PUT", "PATCH"}:
            body = {}  # as the page sends them
        status, _, payload = await http(self.web.port, method, path, body=body)
        return status, as_json(payload)

    def announced(self):
        return [event["what"] for event in iter(lambda: self.events.get_nowait() if not self.events.empty() else None, None)]

    async def test_radio_overview_and_settings(self):
        status, overview = await self.call("GET", "/api/radio")
        self.assertEqual((status, overview["identity"]["name"]), (200, "Base"))
        status, overview = await self.call("PATCH", "/api/radio", {"name": "Hilltop"})
        self.assertEqual((status, overview["identity"]["name"]), (200, "Hilltop"))
        self.assertEqual(self.announced(), ["radio"])
        status, error = await self.call("PATCH", "/api/radio", {"tx_power": 99})
        self.assertEqual(status, 400)
        self.assertIn("TX power", error["error"])

    async def test_radio_actions(self):
        self.assertEqual((await self.call("POST", "/api/radio/advert", {"flood": True}))[0], 204)
        self.assertEqual((await self.call("POST", "/api/radio/advert", {"flood": "yes"}))[0], 400)
        status, body = await self.call("POST", "/api/radio/sync-clock")
        self.assertEqual(status, 200)
        self.assertIn(("set_time", body["device_time"]), self.radio.commands)
        self.assertIn(("send_advert", True), self.radio.commands)

    async def test_disconnected_radio_is_503(self):
        self.radio.is_connected = False
        status, body = await self.call("POST", "/api/radio/channels", {"name": "#local"})
        self.assertEqual(status, 503)
        self.assertIn("isn't connected", body["error"])

    async def test_channels_by_name(self):
        status, channel = await self.call("POST", "/api/radio/channels", {"name": "#local"})
        self.assertEqual((status, channel["index"], channel["kind"]), (201, 1, "hashtag"))
        status, listed = await self.call("GET", "/api/radio/channels")
        self.assertEqual(status, 200)
        by_slot = {c["index"]: c for c in listed["channels"]}
        self.assertEqual(by_slot[1]["secret"], hashtag_secret("#local").hex())
        # Which plugins point at which slot.
        self.assertEqual(by_slot[0]["used_by"], ["Telegram bridge"])
        self.assertEqual(by_slot[1]["used_by"], ["Keyword replies"])
        self.assertEqual(listed["message_max_bytes"], 120)

        status, error = await self.call("POST", "/api/radio/channels", {"name": "#local"})
        self.assertEqual(status, 409)
        status, _ = await self.call("PUT", "/api/radio/channels/1", {"name": "#other"})
        self.assertEqual(status, 200)
        self.assertEqual((await self.call("DELETE", "/api/radio/channels/1"))[0], 204)
        self.assertEqual((await self.call("DELETE", "/api/radio/channels/1"))[0], 404)
        self.assertEqual((await self.call("DELETE", "/api/radio/channels/x"))[0], 404)
        self.assertEqual(self.announced(), ["channels", "channels", "channels"])

    async def test_send_a_channel_message(self):
        status, body = await self.call("POST", "/api/radio/channels/0/messages", {"text": "  hello  "})
        self.assertEqual((status, body), (201, {"id": "mc-out-1"}))
        self.assertEqual((self.sent[0].text, self.sent[0].channel_index), ("hello", 0))
        self.assertEqual((await self.call("POST", "/api/radio/channels/0/messages", {"text": "x" * 121}))[0], 400)
        self.assertEqual((await self.call("POST", "/api/radio/channels/0/messages", {"text": " "}))[0], 400)
        self.assertEqual((await self.call("POST", "/api/radio/channels/2/messages", {"text": "hi"}))[0], 404)

    async def test_contacts(self):
        status, body = await self.call("GET", "/api/radio/contacts")
        self.assertEqual((status, body["contacts"][0]["name"]), (200, "Phone"))
        key = body["contacts"][0]["public_key"]
        self.assertEqual((await self.call("POST", f"/api/radio/contacts/{key}/reset-path"))[0], 204)
        self.assertEqual((await self.call("DELETE", f"/api/radio/contacts/{key}"))[0], 204)
        self.assertEqual((await self.call("DELETE", f"/api/radio/contacts/{key}"))[0], 404)

    async def test_plugins(self):
        status, body = await self.call("GET", "/api/plugins")
        self.assertEqual(status, 200)
        names = [plugin["name"] for plugin in body["plugins"]]
        self.assertEqual(names[:2], ["bridge", "ping_pong"])
        self.assertIn("packet_map", names)

        status, plugin = await self.call("PATCH", "/api/plugins/trace-me", {"enabled": True, "settings": {"keywords": ["route"]}})
        self.assertEqual((status, plugin["name"], plugin["running"]), (200, "trace_me", True))
        self.assertEqual(plugin["settings"], {"keywords": ["route"]})

        status, error = await self.call("PATCH", "/api/plugins/trace_me", {"settings": {"keywords": "route"}})
        self.assertEqual(status, 422)
        self.assertEqual(error["details"], [{"path": "keywords", "message": "must be array"}])
        self.assertEqual((await self.call("PATCH", "/api/plugins/trace_me", {"enabled": "on"}))[0], 400)
        self.assertEqual((await self.call("PATCH", "/api/plugins/trace_me", {"bogus": 1}))[0], 400)
        self.assertEqual((await self.call("PATCH", "/api/plugins/nope", {"enabled": True}))[0], 404)

        status, plugin = await self.call("DELETE", "/api/plugins/trace_me/overrides")
        self.assertEqual((status, plugin["enabled"], plugin["running"]), (200, False, False))
        self.assertEqual(self.announced(), ["plugins", "plugins"])


class HelperTests(unittest.TestCase):
    def test_message_max_bytes_follows_chunking(self):
        settings = MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="c")
        self.assertEqual(message_max_bytes(settings, 140), 120)
        settings.chunking.broadcast_max_chunk_bytes = 0
        settings.chunking.max_chunk_bytes = 0
        self.assertEqual(message_max_bytes(settings, 140), 128)

    def test_channel_usage_defaults_to_the_bridge_channel(self):
        settings = MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="c")
        settings.meshcore.bridge_channel = 2
        plugins = PluginManager(
            [
                PluginConfig("bridge"),
                PluginConfig("trace_me", settings={"channels": [2, 3], "response_channel": 4}),
                PluginConfig("ping_pong", settings={"channels": "1, 3"}),
                PluginConfig("packet_map", enabled=False),
            ],
            _Host(None),
        )
        self.assertEqual(
            dict(channel_usage(plugins, settings)),
            {2: ["Telegram bridge", "Trace replies"], 3: ["Trace replies", "Keyword replies"], 4: ["Trace replies"], 1: ["Keyword replies"]},
        )


if __name__ == "__main__":
    unittest.main()

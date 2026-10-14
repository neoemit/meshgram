import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meshgram.app import MeshgramApp
from meshgram.config import MeshgramSettings, PluginConfig, WebConfig
from meshgram.firmware.catalog import FirmwareCatalog
from meshgram.firmware.updater import FirmwareUpdater
from meshgram.plugin_manager import PluginManager, PluginOverrideStore
from meshgram.radio_admin import RadioAdmin
from meshgram.status import CONNECTED, CONNECTING, StatusRegistry
from meshgram.transport import MeshCoreTransport
from meshgram.web import WebServer
from meshgram.web.api import register_control_api

try:
    from test_firmware_catalog import BOARD_FILES, esp_image, release
    from test_firmware_updater import FakeHost, FakeRunner, FakeTransport
    from test_radio_admin import FakeRadio
    from test_web_server import _Host, as_json, http
except ImportError:
    from tests.test_firmware_catalog import BOARD_FILES, esp_image, release
    from tests.test_firmware_updater import FakeHost, FakeRunner, FakeTransport
    from tests.test_radio_admin import FakeRadio
    from tests.test_web_server import _Host, as_json, http


class FirmwareApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        folder = Path(tempdir.name)
        port = folder / "ttyUSB0"
        port.write_text("")
        self.settings = MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="config.yaml", plugins=[PluginConfig("bridge")])
        self.settings.meshcore.connection.serial_device = str(port)
        image = esp_image()
        raw = [release(version, BOARD_FILES, data=image) for version in ("1.17.1", "1.16.0")]
        self.release_ready = asyncio.Event()
        self.release_ready.set()

        class SlowRunner(FakeRunner):
            async def __call__(runner, spec, on_event, timeout):
                await self.release_ready.wait()
                return await FakeRunner.__call__(runner, spec, on_event, timeout)

        self.transport = FakeTransport()
        self.updater = FirmwareUpdater(
            self.settings, self.transport, FakeHost(self.transport),
            catalog=FirmwareCatalog(folder / "cache", fetch=lambda url: json.dumps(raw).encode(), download=lambda url, target: target.write_bytes(image)),
            store_path=folder / "firmware.json", runner=SlowRunner(),
        )  # fmt: skip
        plugins = PluginManager(self.settings.plugins, _Host(FakeRadio()), PluginOverrideStore(folder / "plugins.json"))
        self.restarts = []
        self.web = WebServer(WebConfig(port=0), StatusRegistry())

        async def send_mesh(action):
            return "id"

        register_control_api(
            self.web, settings=self.settings, admin=RadioAdmin(FakeRadio()), plugins=plugins, send_mesh=send_mesh,
            restart=lambda: self.restarts.append(1), firmware=self.updater,
        )  # fmt: skip
        await self.web.start()
        self.addAsyncCleanup(self.web.stop)
        self.events = self.web.events.subscribe()

    async def call(self, method, path, body=None):
        if body is None and method == "POST":
            body = {}
        status, _, payload = await http(self.web.port, method, path, body=body)
        return status, as_json(payload)

    def published(self):
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    async def test_describe(self):
        status, body = await self.call("GET", "/api/radio/firmware?board=Heltec_v3")
        self.assertEqual(status, 200)
        self.assertEqual((body["board"], body["latest"], body["update_available"]), ("Heltec_v3", "1.17.1", True))
        self.assertEqual(body["device"]["version"], "v1.16.0")
        self.assertIsNone(body["job"])
        status, body = await self.call("GET", "/api/radio/firmware?board=Nope")
        self.assertEqual(status, 404)

    async def test_update_runs_and_reports_progress(self):
        self.release_ready.clear()
        status, job = await self.call("POST", "/api/radio/firmware/update", {"version": "1.17.1", "board": "Heltec_v3"})
        self.assertEqual((status, job["state"]), (202, "running"))
        # No restart while flashing.
        status, body = await self.call("POST", "/api/restart")
        self.assertEqual(status, 409)
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.web.events.snapshot()["firmware"]["id"], job["id"])
        status, body = await self.call("POST", "/api/radio/firmware/update", {"version": "1.17.1", "board": "Heltec_v3"})
        self.assertEqual(status, 409)

        self.release_ready.set()
        await self.updater._task
        events = self.published()
        jobs = [event["job"] for event in events if event["type"] == "firmware"]
        self.assertEqual(jobs[-1]["state"], "succeeded")
        self.assertIn(50, [job["percent"] for job in jobs])
        self.assertEqual([event["what"] for event in events if event["type"] == "control"], ["radio"])

    async def test_bad_requests(self):
        status, body = await self.call("POST", "/api/radio/firmware/update", {"version": "1.17.1", "board": "Heltec_v3", "force": True})
        self.assertEqual(status, 400)
        status, body = await self.call("POST", "/api/radio/firmware/update", {"version": "1.16.0", "board": "RAK_4631"})
        self.assertEqual(status, 409)
        status, body = await self.call("POST", "/api/radio/firmware/update", {"board": "Heltec_v3"})
        self.assertEqual(status, 400)


class _FakeMc:
    def __init__(self):
        self.is_connected = True
        self.commands = object()
        self.calls = []

    def unsubscribe(self, sub):
        pass

    async def stop_auto_message_fetching(self):
        self.calls.append("stop")

    async def disconnect(self):
        await asyncio.sleep(0)
        self.calls.append("disconnect")


class RadioMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": data_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="config.yaml")
        settings.web.enabled = False
        self.app = MeshgramApp(settings)

    async def test_transport_disconnect_waits_until_closed(self):
        transport = MeshCoreTransport(self.app.settings)
        transport._mc = mc = _FakeMc()
        await transport.disconnect()
        self.assertEqual(mc.calls, ["stop", "disconnect"])
        self.assertFalse(transport.is_connected)
        await transport.disconnect()  # nothing left to do
        self.assertEqual(transport.serial_port, None)
        self.app.settings.meshcore.connection.serial_device = "/dev/ttyACM0"
        self.assertEqual(transport.serial_port, "/dev/ttyACM0")

    async def test_maintenance_keeps_the_radio_offline_then_reconnects(self):
        connects = []
        mesh = self.app.mesh

        async def connect(loop, on_text):
            connects.append(1)
            mesh._mc = _FakeMc()

        with mock.patch.object(mesh, "connect", connect), mock.patch.object(self.app.plugins, "mesh_connected", mock.AsyncMock()):
            self.assertFalse(await self.app._connect_mesh(5))
            self.assertEqual(self.app.radio_generation, 1)
            self.assertEqual(self.app.status.get("radio")["state"], CONNECTED)

            async with self.app.radio_maintenance("Updating the firmware"):
                self.assertFalse(mesh.is_connected)
                self.assertEqual(self.app.status.get("radio")["state"], CONNECTING)
                self.assertTrue(self.app._radio_lock.locked())
                # The connect loop waits.
                loop_task = asyncio.create_task(self.app._ensure_mesh_connected())
                await asyncio.sleep(0.05)
                self.assertEqual(len(connects), 1)
            self.assertTrue(await self.app.wait_for_radio(1, 2))
            self.assertEqual((len(connects), self.app.radio_generation), (2, 2))
            loop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop_task
        self.assertFalse(await self.app.wait_for_radio(2, 0.01))

    async def test_restart_refused_while_updating(self):
        self.app.bot_app = object()
        with mock.patch.object(type(self.app.firmware), "busy", new_callable=mock.PropertyMock, return_value=True):
            with self.assertRaisesRegex(RuntimeError, "being updated"):
                self.app.request_restart()


if __name__ == "__main__":
    unittest.main()

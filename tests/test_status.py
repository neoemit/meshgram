import asyncio
import os
import tempfile
import threading
import unittest
import unittest.mock
from types import SimpleNamespace
from unittest import mock

from telegram.error import NetworkError

from meshgram.app import MeshgramApp
from meshgram.config import MeshgramSettings
from meshgram.status import StatusRegistry


class StatusRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_order_and_since(self):
        status = StatusRegistry()
        status.set_state("mqtt_subscribe", "disabled", "off", label="Sub")
        status.set_state("radio", "connecting", label="Radio")
        status.set_state("custom", "connected")
        self.assertEqual([entry["key"] for entry in status.snapshot()], ["radio", "mqtt_subscribe", "custom"])

        since = status.get("radio")["since"]
        status.set_state("radio", "connecting", "still trying")
        self.assertEqual(status.get("radio")["since"], since)  # same state keeps its start time
        self.assertEqual(status.get("radio")["label"], "Radio")  # label is remembered
        self.assertEqual(status.get("custom")["label"], "custom")

        with self.assertRaises(ValueError):
            status.set_state("radio", "bogus")

    async def test_listeners_run_on_their_loop_and_skip_unchanged(self):
        status = StatusRegistry()
        events = []
        loop_thread = []

        def listener(entry):
            loop_thread.append(threading.get_ident())
            events.append((entry["key"], entry["state"]))

        status.add_listener(listener)
        status.set_state("radio", "connecting")
        # Updates from another thread (paho's network thread) reach the loop too.
        worker = threading.Thread(target=status.set_state, args=("radio", "connected"))
        worker.start()
        worker.join()
        status.set_state("radio", "connected")  # unchanged: no event
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(events, [("radio", "connecting"), ("radio", "connected")])
        self.assertEqual(set(loop_thread), {threading.get_ident()})

        status.remove_listener(listener)
        status.set_state("radio", "disconnected")
        await asyncio.sleep(0)
        self.assertEqual(len(events), 2)

    async def test_remove_forgets_a_service_and_tells_listeners(self):
        status = StatusRegistry()
        events = []
        status.add_listener(events.append)
        status.set_state("mqtt_publish", "connected", label="MQTT")
        status.remove("mqtt_publish")
        status.remove("mqtt_publish")  # already gone: nothing to tell
        await asyncio.sleep(0)
        self.assertIsNone(status.get("mqtt_publish"))
        self.assertEqual(events[-1], {"key": "mqtt_publish", "removed": True})
        self.assertEqual(len(events), 2)


class _FlakyMesh:
    def __init__(self):
        self.is_connected = False
        self.attempts = 0

    async def connect(self, loop, on_text):
        self.attempts += 1
        if self.attempts == 1:
            raise ConnectionError("no device")
        self.is_connected = True

    def invalidate_connection(self):
        self.is_connected = False

    def refresh_local_node_id(self):
        pass

    payload_limit = 200
    local_node_id = None


class AppStatusTests(unittest.IsolatedAsyncioTestCase):
    def _app(self) -> MeshgramApp:
        settings = MeshgramSettings(telegram_bot_token="token", telegram_group_id=-1, config_path="config.yaml", plugins=[])
        settings.meshcore.connection.mode = "tcp"
        settings.meshcore.connection.tcp_host = "radio.local"
        settings.web.enabled = False
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        with mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": data_dir.name}):
            return MeshgramApp(settings)

    async def test_initial_states(self):
        app = self._app()
        self.assertEqual(app.status.get("radio")["state"], "connecting")
        self.assertEqual(app.status.get("radio")["detail"], "meshcore tcp radio.local:5000")
        self.assertEqual(app.status.get("telegram")["state"], "connecting")

    async def test_radio_status_follows_connection(self):
        app = self._app()
        mesh = _FlakyMesh()
        app.mesh = mesh
        sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await sleep(0)

        states = []
        app.status.add_listener(lambda entry: states.append(entry["state"]) if entry["key"] == "radio" else None)
        with unittest.mock.patch("meshgram.app.asyncio.sleep", fast_sleep):
            with self.assertLogs("meshgram.app", level="WARNING"):
                task = asyncio.create_task(app._ensure_mesh_connected())
                for _ in range(10):
                    await sleep(0)
            self.assertEqual(app.status.get("radio")["state"], "connected")
            mesh.is_connected = False  # link drops; reconnect succeeds
            for _ in range(10):
                await sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(states[:4], ["disconnected", "connected", "disconnected", "connected"])

    async def test_telegram_polling_errors_and_recovery(self):
        app = self._app()
        bot = SimpleNamespace(username="meshgram_bot")
        with self.assertLogs("meshgram.app", level="WARNING"):
            await app._on_telegram_error(None, SimpleNamespace(error=NetworkError("timed out"), bot=bot))
        self.assertEqual(app.status.get("telegram")["state"], "disconnected")
        await app._on_telegram_update(object(), SimpleNamespace(bot=bot))
        self.assertEqual(app.status.get("telegram")["state"], "connected")
        self.assertEqual(app.status.get("telegram")["detail"], "@meshgram_bot")


if __name__ == "__main__":
    unittest.main()

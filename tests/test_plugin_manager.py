import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from meshgram.config import PluginConfig
from meshgram.plugin import BasePlugin
from meshgram.plugin_manager import PluginManager, PluginOverrideStore, UnknownPluginError
from meshgram.settings_schema import SECRET_MASK, SettingsError
from meshgram.types import SendTelegramAction

EVENTS: list[tuple] = []


class RecordingPlugin(BasePlugin):
    name = "recording"
    title = "Recording"
    description = "Records its lifecycle."
    settings_schema = {
        "type": "object",
        "properties": {
            "greeting": {"type": "string"},
            "token": {"type": "string", "writeOnly": True},
        },
    }

    async def on_startup(self, context):
        EVENTS.append(("startup", self.settings.get("greeting")))
        if self.settings.get("greeting") == "explode":
            raise RuntimeError("boom")
        return [SendTelegramAction(chat_id=1, text=f"hello {self.settings.get('greeting')}")]

    async def on_mesh_connected(self, transport, context):
        EVENTS.append(("connected", self.settings.get("greeting")))

    async def on_shutdown(self):
        EVENTS.append(("shutdown", self.settings.get("greeting")))


TARGET = f"{__name__}:RecordingPlugin"


class _Host:
    def __init__(self):
        self.actions = []
        self.transport = None

    def plugin_context(self):
        return SimpleNamespace(web=None, status=None)

    async def execute_actions(self, actions, plugin_name):
        self.actions.extend((plugin_name, action.text) for action in actions)

    def connected_transport(self):
        return self.transport


class PluginManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        EVENTS.clear()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "plugins.json"
        self.host = _Host()

    def _manager(self, enabled=True, settings=None) -> PluginManager:
        configs = [PluginConfig(name=TARGET, enabled=enabled, settings=settings or {"greeting": "config", "token": "s3cret"})]
        return PluginManager(configs, self.host, PluginOverrideStore(self.path))

    async def test_starts_enabled_plugins_and_lists_every_builtin(self):
        manager = self._manager()
        await manager.start_all()
        self.assertEqual([name for name, _ in manager.running], [TARGET])
        self.assertEqual(EVENTS, [("startup", "config")])
        self.assertEqual(self.host.actions, [(TARGET, "hello config")])

        catalog = {plugin["name"]: plugin for plugin in manager.catalog()}
        self.assertEqual(catalog[TARGET]["title"], "Recording")
        self.assertTrue(catalog[TARGET]["running"])
        self.assertFalse(catalog[TARGET]["builtin"])
        # Built-ins config.yaml doesn't mention are there, off, ready to turn on.
        self.assertFalse(catalog["packet_map"]["enabled"])
        self.assertFalse(catalog["packet_map"]["in_config"])
        self.assertIn("properties", catalog["packet_map"]["schema"])

        await manager.stop_all()
        self.assertEqual(EVENTS[-1], ("shutdown", "config"))
        self.assertEqual(manager.running, [])

    async def test_secrets_are_masked(self):
        manager = self._manager()
        self.assertEqual(manager.describe(TARGET)["settings"], {"greeting": "config", "token": SECRET_MASK})

    async def test_turning_on_and_off_at_runtime(self):
        manager = self._manager(enabled=False)
        await manager.start_all()
        self.assertEqual(manager.running, [])
        self.host.transport = object()

        plugin = await manager.update(TARGET, enabled=True)
        self.assertTrue(plugin["running"])
        self.assertEqual(plugin["overridden"], ["enabled"])
        # Started while the radio is connected: it hears about it right away.
        self.assertEqual(EVENTS, [("startup", "config"), ("connected", "config")])

        plugin = await manager.update(TARGET, enabled=False)
        self.assertFalse(plugin["running"])
        self.assertEqual(EVENTS[-1], ("shutdown", "config"))
        # Back to what config.yaml says: nothing left to save.
        self.assertEqual(plugin["overridden"], [])
        self.assertEqual(json.loads(self.path.read_text())["plugins"], {})

    async def test_new_settings_restart_the_plugin_and_persist(self):
        manager = self._manager()
        await manager.start_all()
        plugin = await manager.update(TARGET, settings={"greeting": "web", "token": SECRET_MASK})
        self.assertEqual(EVENTS, [("startup", "config"), ("shutdown", "config"), ("startup", "web")])
        self.assertEqual(plugin["overridden"], ["settings"])
        self.assertEqual(plugin["settings"]["token"], SECRET_MASK)

        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["plugins"][TARGET]["settings"], {"greeting": "web", "token": "s3cret"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

        # The next run starts from the saved settings.
        EVENTS.clear()
        with self.assertLogs("meshgram.plugin_manager", "INFO"):
            restarted = self._manager()
        await restarted.start_all()
        self.assertEqual(EVENTS, [("startup", "web")])

        # Reset: config.yaml again.
        plugin = await restarted.reset(TARGET)
        self.assertEqual(plugin["overridden"], [])
        self.assertEqual(EVENTS[-2:], [("shutdown", "web"), ("startup", "config")])
        self.assertEqual(json.loads(self.path.read_text())["plugins"], {})
        await restarted.stop_all()
        await manager.stop_all()

    async def test_invalid_settings_change_nothing(self):
        manager = self._manager()
        await manager.start_all()
        with self.assertRaises(SettingsError):
            await manager.update(TARGET, settings={"greeting": 5})
        with self.assertRaises(SettingsError):
            await manager.update(TARGET, settings="nope")
        self.assertEqual(EVENTS, [("startup", "config")])
        self.assertFalse(self.path.exists())

    async def test_unsaved_change_is_not_applied(self):
        manager = self._manager()
        await manager.start_all()
        self.path.mkdir()  # can't write the file
        with self.assertRaises(OSError):
            await manager.update(TARGET, settings={"greeting": "web"})
        self.assertEqual(EVENTS, [("startup", "config")])
        self.assertEqual(manager.describe(TARGET)["overridden"], [])

    async def test_a_plugin_that_fails_to_start_reports_why(self):
        manager = self._manager(settings={"greeting": "explode"})
        with self.assertLogs("meshgram.plugin_manager", "ERROR"):
            await manager.start_all()
        plugin = manager.describe(TARGET)
        self.assertFalse(plugin["running"])
        self.assertIn("boom", plugin["error"])
        # What it set up is released.
        self.assertEqual(EVENTS, [("startup", "explode"), ("shutdown", "explode")])
        # Fixing its settings starts it.
        plugin = await manager.update(TARGET, settings={"greeting": "fixed"})
        self.assertTrue(plugin["running"])
        self.assertIsNone(plugin["error"])
        await manager.stop_all()

    async def test_unknown_or_unloadable_plugins(self):
        manager = self._manager()
        with self.assertRaises(UnknownPluginError):
            await manager.update("nope", enabled=True)
        with self.assertLogs("meshgram.plugin_manager", "ERROR"):
            broken = PluginManager([PluginConfig(name="no.such.module")], self.host)
        self.assertIn("Can't load", broken.describe("no.such.module")["error"])
        await broken.start_all()
        self.assertEqual(broken.running, [])

    async def test_aliases_and_duplicates(self):
        with self.assertLogs("meshgram.plugin_manager", "WARNING"):
            manager = PluginManager(
                [PluginConfig(name="trace-me", settings={"keywords": ["a"]}), PluginConfig(name="trace_me")], self.host
            )
        self.assertEqual(manager.describe("trace_me")["settings"], {"keywords": ["a"]})
        self.assertEqual([plugin["name"] for plugin in manager.catalog()].count("trace_me"), 1)


if __name__ == "__main__":
    unittest.main()

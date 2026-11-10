import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from meshgram.config import PluginConfig
from meshgram.plugin import BasePlugin
from meshgram.plugin import plugin_key, resolve_plugin_target, split_instance
from meshgram.plugin_manager import PluginInstanceError, PluginManager, PluginOverrideStore, UnknownPluginError
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


class LivePlugin(RecordingPlugin):
    """Takes a new greeting while running; anything else needs a restart."""

    async def apply_settings(self, settings):
        if settings.get("greeting") == "crash":
            raise RuntimeError("bad")
        if set(settings) - {"greeting", "token"}:
            return False
        EVENTS.append(("applied", settings.get("greeting")))
        self.settings = settings
        return True


TARGET = f"{__name__}:RecordingPlugin"
LIVE_TARGET = f"{__name__}:LivePlugin"


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

    async def test_saved_changes_config_yaml_has_caught_up_with_are_dropped(self):
        manager = self._manager(enabled=False)
        await manager.update(TARGET, enabled=True, settings={"greeting": "web", "token": SECRET_MASK})
        self.assertEqual(manager.overrides(), {TARGET: {"enabled": True, "settings": {"greeting": "web", "token": "s3cret"}}})

        # config.yaml now has the settings (copied from the export), not the on/off state.
        with self.assertLogs("meshgram.plugin_manager", "INFO") as logs:
            restarted = self._manager(enabled=False, settings={"greeting": "web", "token": "s3cret"})
        self.assertIn("now has: dropped (" + TARGET + ")", "\n".join(logs.output))
        self.assertEqual(restarted.overrides(), {TARGET: {"enabled": True}})
        self.assertEqual(restarted.describe(TARGET)["overridden"], ["enabled"])
        self.assertEqual(json.loads(self.path.read_text())["plugins"], {TARGET: {"enabled": True}})

        # And the on/off state too: nothing left.
        with self.assertLogs("meshgram.plugin_manager", "INFO"):
            caught_up = self._manager(enabled=True, settings={"greeting": "web", "token": "s3cret"})
        self.assertEqual(caught_up.overrides(), {})
        self.assertEqual(json.loads(self.path.read_text())["plugins"], {})
        await manager.stop_all()

    async def test_plugins_that_can_take_settings_live_keep_running(self):
        manager = PluginManager([PluginConfig(name=LIVE_TARGET, settings={"greeting": "config"})], self.host, PluginOverrideStore(self.path))
        await manager.start_all()
        plugin = await manager.update(LIVE_TARGET, settings={"greeting": "live"})
        self.assertFalse(plugin["restarted"])
        self.assertTrue(plugin["running"])
        self.assertEqual(EVENTS, [("startup", "config"), ("applied", "live")])

        # Same settings again: nothing to do.
        plugin = await manager.update(LIVE_TARGET, settings={"greeting": "live"})
        self.assertEqual(EVENTS[-1], ("applied", "live"))

        # It can't take these live: restarted.
        plugin = await manager.update(LIVE_TARGET, settings={"greeting": "live", "other": 1})
        self.assertTrue(plugin["restarted"])
        self.assertEqual(EVENTS[-2:], [("shutdown", "live"), ("startup", "live")])

        # A failing live change falls back to a restart.
        with self.assertLogs("meshgram.plugin_manager", "ERROR"):
            plugin = await manager.update(LIVE_TARGET, settings={"greeting": "crash"})
        self.assertTrue(plugin["restarted"])
        self.assertEqual(EVENTS[-1], ("startup", "crash"))

        # Resetting applies config.yaml's settings the same way.
        plugin = await manager.reset(LIVE_TARGET)
        self.assertEqual((plugin["restarted"], EVENTS[-1]), (False, ("applied", "config")))
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


class PluginInstanceTests(unittest.IsolatedAsyncioTestCase):
    """More instances of a plugin that allows it: "bridge@sos"."""

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "plugins.json"
        self.host = _Host()
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": tempdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manager(self, configs=None) -> PluginManager:
        return PluginManager(configs if configs is not None else [PluginConfig("bridge")], self.host, PluginOverrideStore(self.path))

    def test_names(self):
        self.assertEqual(split_instance("bridge@sos"), ("bridge", "sos"))
        self.assertEqual(split_instance("trace-me"), ("trace_me", None))
        self.assertEqual(plugin_key("Bridge@SOS".lower()), "bridge@sos")
        self.assertEqual(plugin_key("bridge@my-sos"), "bridge@my_sos")
        self.assertEqual(resolve_plugin_target("bridge@sos"), "meshgram.plugins.bridge:BridgePlugin")
        # Only built-ins have instances; any other name is a module path, taken whole.
        self.assertEqual(split_instance("my.module@x:Plugin"), ("my.module@x:Plugin", None))

    async def test_instances_from_config_run_next_to_the_plugin(self):
        manager = self._manager([
            PluginConfig("bridge", settings={"channel": 0}),
            PluginConfig("bridge@sos", settings={"channel": 5, "telegram_group_id": -5}),
        ])
        await manager.start_all()
        self.addAsyncCleanup(manager.stop_all)
        running = dict(manager.running)
        self.assertEqual(list(running), ["bridge", "bridge@sos"])
        self.assertIsNone(running["bridge"].instance_id)
        self.assertEqual(running["bridge@sos"].instance_id, "sos")
        self.assertEqual(running["bridge@sos"].instance_name, "bridge@sos")

        described = manager.describe("bridge@sos")
        self.assertEqual(
            (described["title"], described["base"], described["instance"], described["removable"], described["builtin"]),
            ("Telegram bridge (sos)", "bridge", "sos", False, True),
        )
        self.assertTrue(manager.describe("bridge")["multi_instance"])
        self.assertFalse(manager.describe("ping_pong")["multi_instance"])
        # One from config.yaml is removed there.
        with self.assertRaises(PluginInstanceError):
            await manager.remove_instance("bridge@sos")

    async def test_a_plugin_that_runs_once_has_no_instances(self):
        with self.assertLogs("meshgram.plugin_manager", "ERROR"):
            manager = self._manager([PluginConfig("ping_pong@two"), PluginConfig("bridge@Not OK")])
        self.assertIn("can't run more than once", manager.describe("ping_pong@two")["error"])
        self.assertIn("instance name", manager.describe("bridge@not ok")["error"])
        await manager.start_all()
        self.assertEqual(manager.running, [])
        with self.assertRaises(PluginInstanceError):
            await manager.add_instance("ping_pong", "two")
        with self.assertRaises(UnknownPluginError):
            await manager.add_instance("nope", "two")

    async def test_adding_configuring_and_removing_an_instance(self):
        manager = self._manager()
        await manager.start_all()
        self.addAsyncCleanup(manager.stop_all)

        for bad in ("", "has space", "x" * 25, "_lead"):
            with self.assertRaises(PluginInstanceError, msg=bad):
                await manager.add_instance("bridge", bad)
        added = await manager.add_instance("bridge", "SOS")
        self.assertEqual((added["name"], added["enabled"], added["running"], added["removable"]), ("bridge@sos", False, False, True))
        with self.assertRaises(PluginInstanceError) as caught:
            await manager.add_instance("bridge", "sos")
        self.assertEqual(caught.exception.status, 409)
        # Listed right after the plugin it's an instance of.
        names = [plugin["name"] for plugin in manager.catalog()]
        self.assertEqual(names.index("bridge@sos"), names.index("bridge") + 1)

        # Turned on without a channel it refuses to start: it would double the main bridge.
        with self.assertLogs("meshgram.plugin_manager", "ERROR"):
            failed = await manager.update("bridge@sos", enabled=True)
        self.assertIn("radio channel", failed["error"])
        updated = await manager.update("bridge@sos", settings={"channel": 5, "telegram_group_id": -5})
        self.assertTrue(updated["running"])
        self.assertEqual([name for name, _ in manager.running], ["bridge", "bridge@sos"])
        with self.assertRaises(PluginInstanceError):
            await manager.reset("bridge@sos")

        # It's still there, as it was, after a restart; turned off too.
        again = self._manager()
        self.assertEqual(again.describe("bridge@sos")["settings"], {"channel": 5, "telegram_group_id": -5})
        self.assertTrue(again.describe("bridge@sos")["enabled"])
        await manager.update("bridge@sos", enabled=False)
        self.assertFalse(self._manager().describe("bridge@sos")["enabled"])
        self.assertEqual(self._manager().overrides()["bridge@sos"]["enabled"], False)

        await manager.remove_instance("bridge@sos")
        self.assertEqual([name for name, _ in manager.running], ["bridge"])
        with self.assertRaises(UnknownPluginError):
            manager.describe("bridge@sos")
        with self.assertRaises(UnknownPluginError):
            self._manager().describe("bridge@sos")
        with self.assertRaises(PluginInstanceError):
            await manager.remove_instance("bridge")


if __name__ == "__main__":
    unittest.main()

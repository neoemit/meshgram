import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from meshgram.config import ConfigError, build_settings, with_overrides
from meshgram.core_settings import (
    LEAVES,
    SCHEMA,
    CoreSettings,
    SettingsOverrideStore,
    UnknownSectionError,
    core_values,
    flatten,
)
from meshgram.settings_schema import SECRET_MASK, SettingsError, validate

CONFIG = {
    "telegram": {"bot_token": "123:abc", "group_id": "-100123"},
    "meshcore": {
        "contact_name_overrides": {"aa11": "Alice"},
        "connection": {"mode": "serial", "serial_device": "/dev/ttyACM0"},
    },
}


class CoreSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "settings.json"
        self.store = SettingsOverrideStore(self.path)
        self.checked = []

    def core(self, config=CONFIG, **kwargs):
        async def check(token, group_id):
            self.checked.append((token, group_id))
            if token == "999:rejected":
                raise SettingsError([("bot_token", "Telegram doesn't accept this token")])

        return CoreSettings(config, config_path="config.yaml", store=self.store, check_telegram=check, **kwargs)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["settings"]

    def section(self, described, key):
        return next(section for section in described["sections"] if section["key"] == key)

    def test_the_schema_covers_every_setting(self):
        values = core_values(build_settings(CONFIG))
        self.assertEqual(set(flatten(values)), set(LEAVES))
        # Settings as Meshgram reads them fit the schema the control panel checks against.
        for key in SCHEMA["properties"]:
            validate(values[key], SCHEMA["properties"][key])

        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        values = core_values(build_settings(yaml.safe_load(example.read_text(encoding="utf-8"))))
        for key in SCHEMA["properties"]:
            validate(values[key], SCHEMA["properties"][key])

    def test_without_changes_it_is_config_yaml(self):
        core = self.core()
        self.assertEqual(core.settings, build_settings(CONFIG))
        self.assertEqual((core.overrides(), core.pending()), ({}, []))
        self.assertFalse(self.path.exists())

    async def test_changes_are_saved_and_used_from_the_next_start(self):
        core = self.core()
        values = core.values()["meshcore"]
        values["connection"]["serial_device"] = "/dev/ttyUSB0"
        values["contact_name_overrides"] = {"bb22": "Bob"}
        described = await core.update("meshcore", values)

        expected = {"meshcore.connection.serial_device": "/dev/ttyUSB0", "meshcore.contact_name_overrides": {"bb22": "Bob"}}
        self.assertEqual(self.saved(), expected)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        section = self.section(described, "meshcore")
        self.assertEqual(sorted(section["overridden"]), ["connection.serial_device", "contact_name_overrides"])
        self.assertEqual(section["pending"], section["overridden"])
        self.assertTrue(described["restart_needed"])
        # Still running with what it started with.
        self.assertEqual(core.settings.meshcore.connection.serial_device, "/dev/ttyACM0")

        restarted = self.core()
        self.assertEqual(restarted.settings.meshcore.connection.serial_device, "/dev/ttyUSB0")
        # A map is replaced, not merged with config.yaml's.
        self.assertEqual(restarted.settings.meshcore.contact_name_overrides, {"bb22": "Bob"})
        self.assertEqual(restarted.pending(), [])

    async def test_only_what_differs_from_config_yaml_is_saved(self):
        core = self.core()
        values = core.values()["telegram"]
        values["bot_token"] = SECRET_MASK  # as the page sends it back
        values["group_id"] = -100123  # config.yaml has "-100123"
        await core.update("telegram", values)
        self.assertEqual(core.overrides(), {})
        self.assertEqual(self.checked, [])

        # Left out: the default, as when config.yaml leaves it out.
        values = core.values()["meshcore"]
        del values["connection"]["serial_device"]
        await core.update("meshcore", values)
        self.assertEqual(core.overrides(), {"meshcore.connection.serial_device": ""})
        self.assertIsNone(self.core().settings.meshcore.connection.serial_device)

    async def test_secrets_are_masked_and_checked_with_telegram(self):
        core = self.core()
        described = core.describe()
        self.assertEqual(self.section(described, "telegram")["values"]["bot_token"], SECRET_MASK)
        # Unset secrets aren't masked.
        self.assertEqual(self.section(described, "web")["values"]["password"], "")

        values = {**core.values()["telegram"], "bot_token": "999:rejected"}
        with self.assertRaises(SettingsError) as caught:
            await core.update("telegram", values)
        self.assertEqual(caught.exception.errors, [("bot_token", "Telegram doesn't accept this token")])
        self.assertEqual(core.overrides(), {})

        values["bot_token"] = "456:new"
        await core.update("telegram", values)
        self.assertEqual(self.checked[-1], ("456:new", -100123))
        self.assertEqual(self.saved(), {"telegram.bot_token": "456:new"})
        self.assertEqual(self.core().settings.telegram_bot_token, "456:new")

        # Clearing a required secret isn't allowed; a malformed token isn't even sent to Telegram.
        checks = len(self.checked)
        for token, message in (("", "must not be empty"), ("nope", "looks like")):
            with self.assertRaises(SettingsError) as caught:
                await core.update("telegram", {**values, "bot_token": token})
            self.assertEqual(caught.exception.errors[0][0], "bot_token")
            self.assertIn(message, caught.exception.errors[0][1])
        self.assertEqual(len(self.checked), checks)

    async def test_invalid_values_are_refused(self):
        core = self.core()
        values = core.values()["web"]
        with self.assertRaises(SettingsError) as caught:
            await core.update("web", {**values, "port": 70000, "bogus": 1})
        self.assertEqual(
            sorted(caught.exception.errors), [("bogus", "isn't a known setting"), ("port", "must be at most 65535")]
        )
        with self.assertRaises(SettingsError):
            await core.update("meshcore", {"connection": {"mode": "carrier-pigeon"}})
        with self.assertRaises(UnknownSectionError):
            await core.update("plugins", {})

    async def test_reset_goes_back_to_config_yaml(self):
        core = self.core()
        await core.update("runtime", {"log_level": "DEBUG"})
        await core.update("chunking", {**core.values()["chunking"], "max_chunk_bytes": 100})
        described = await core.reset("runtime")
        self.assertEqual(self.saved(), {"chunking.max_chunk_bytes": 100})
        self.assertEqual(self.section(described, "runtime")["overridden"], [])

    def test_changes_config_yaml_now_has_are_dropped(self):
        self.store.save({
            "meshcore.connection.serial_device": "/dev/ttyACM0",  # config.yaml says so now
            "web.port": 9000,
            "web.nonsense": 1,  # no such setting
            "chunking.retry_max_attempts": 0,  # not valid
        })
        core = self.core()
        self.assertEqual(core.overrides(), {"web.port": 9000})
        self.assertEqual(self.saved(), {"web.port": 9000})
        self.assertEqual(core.settings.web.port, 9000)

    def test_telegram_settings_can_come_from_the_control_panel(self):
        config = {"meshcore": {}}
        with self.assertRaisesRegex(ConfigError, "bot_token is required"):
            self.core(config)
        self.store.save({"telegram.bot_token": "123:abc", "telegram.group_id": -1})
        core = self.core(config)
        self.assertEqual((core.settings.telegram_bot_token, core.settings.telegram_group_id), ("123:abc", -1))

    def test_web_changes_keep_the_old_packet_map_web_settings(self):
        config = {**CONFIG, "plugins": [{"name": "packet_map", "settings": {"port": 9000, "password": "pw"}}]}
        self.store.save({"web.title": "Hilltop"})
        web = self.core(config).settings.web
        self.assertEqual((web.title, web.port, web.password), ("Hilltop", 9000, "pw"))


class WithOverridesTests(unittest.TestCase):
    def test_sets_dotted_paths_on_a_copy(self):
        data = {"meshcore": {"connection": {"mode": "tcp"}}}
        merged = with_overrides(data, {"meshcore.connection.mode": "serial", "runtime.log_level": "DEBUG"})
        self.assertEqual(merged, {"meshcore": {"connection": {"mode": "serial"}}, "runtime": {"log_level": "DEBUG"}})
        self.assertEqual(data, {"meshcore": {"connection": {"mode": "tcp"}}})


if __name__ == "__main__":
    unittest.main()

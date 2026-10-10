import unittest

from meshgram.plugin import load_plugin_class, BUILTIN_PLUGINS
from meshgram.settings_schema import SECRET_MASK, SettingsError, mask_secrets, restore_secrets, validate

SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 8},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "ratio": {"type": "number", "exclusiveMinimum": 0},
        "on": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "code": {"type": "string", "pattern": "^[A-Z]{3}$", "x-pattern-message": "must be three letters"},
        "channels": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True},
        "password": {"type": "string", "writeOnly": True},
        "commands": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string", "writeOnly": True}},
                },
            },
        },
    },
}


class ValidateTests(unittest.TestCase):
    def test_valid_settings_pass(self):
        validate(
            {
                "name": "x",
                "count": 3,
                "ratio": 0.5,
                "on": False,
                "mode": "b",
                "code": "YUL",
                "channels": [0, 1],
                "commands": {"BAT": {"url": "http://x", "headers": {"K": "v"}}},
                "unknown_extra": "kept",
            },
            SCHEMA,
        )

    def test_errors_name_the_setting(self):
        with self.assertRaises(SettingsError) as caught:
            validate(
                {
                    "count": True,  # a bool isn't an integer
                    "ratio": 0,
                    "mode": "c",
                    "code": "yul",
                    "channels": [1, 1],
                    "commands": {"BAT": {"headers": {"K": 5}}},
                },
                SCHEMA,
            )
        errors = dict(caught.exception.errors)
        self.assertEqual(errors["name"], "is required")
        self.assertEqual(errors["count"], "must be integer")
        self.assertEqual(errors["ratio"], "must be more than 0")
        self.assertIn("must be one of", errors["mode"])
        self.assertEqual(errors["code"], "must be three letters")
        self.assertEqual(errors["channels"], "must not repeat items")
        self.assertEqual(errors["commands.BAT.url"], "is required")
        self.assertEqual(errors["commands.BAT.headers.K"], "must be string")

    def test_ranges_and_lengths(self):
        with self.assertRaises(SettingsError) as caught:
            validate({"name": "", "count": 9}, SCHEMA)
        self.assertEqual(dict(caught.exception.errors), {"name": "must not be empty", "count": "must be at most 5"})

    def test_builtin_plugin_schemas_accept_their_defaults(self):
        for name in BUILTIN_PLUGINS:
            schema = load_plugin_class(name).settings_schema
            defaults = {key: prop["default"] for key, prop in schema.get("properties", {}).items() if "default" in prop}
            required = {key: "YUL" for key in schema.get("required", [])}
            with self.subTest(plugin=name):
                validate({**defaults, **required}, schema)


class SecretTests(unittest.TestCase):
    SETTINGS = {
        "name": "x",
        "password": "hunter2",
        "commands": {"BAT": {"url": "http://x", "headers": {"X-Key": "abc", "Empty": ""}}},
    }

    def test_mask_hides_set_secrets_only(self):
        masked = mask_secrets(self.SETTINGS, SCHEMA)
        self.assertEqual(masked["password"], SECRET_MASK)
        self.assertEqual(masked["commands"]["BAT"]["headers"], {"X-Key": SECRET_MASK, "Empty": ""})
        self.assertEqual(masked["commands"]["BAT"]["url"], "http://x")
        self.assertEqual(self.SETTINGS["password"], "hunter2")  # a copy

    def test_restore_puts_unchanged_secrets_back(self):
        edited = mask_secrets(self.SETTINGS, SCHEMA)
        edited["name"] = "y"
        edited["commands"]["BAT"]["headers"]["New"] = "typed"
        restored = restore_secrets(edited, self.SETTINGS, SCHEMA)
        self.assertEqual(restored["password"], "hunter2")
        self.assertEqual(restored["commands"]["BAT"]["headers"], {"X-Key": "abc", "Empty": "", "New": "typed"})
        self.assertEqual(restored["name"], "y")

    def test_a_mask_without_a_stored_secret_must_be_retyped(self):
        edited = mask_secrets(self.SETTINGS, SCHEMA)
        edited["commands"] = {"RENAMED": edited["commands"]["BAT"]}
        with self.assertRaises(SettingsError) as caught:
            restore_secrets(edited, self.SETTINGS, SCHEMA)
        self.assertEqual(caught.exception.errors, [("commands.RENAMED.headers.X-Key", "enter the secret again")])


if __name__ == "__main__":
    unittest.main()

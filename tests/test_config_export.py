import textwrap
import unittest
from pathlib import Path

import yaml

from meshgram.config import build_settings
from meshgram.config_export import ConfigExportError, export_config

CONFIG = textwrap.dedent(
    """\
    # Meshgram settings.
    telegram:
      bot_token: "123:abc"
      group_id: -100123

    plugins:
      - name: bridge
        enabled: true

      - name: ping_pong
        settings:
          keyword_responses:
            Ping: "Pong"
            Ack: "Ack"
          # Introduces the dedupe mode.
          response_dedupe_mode: packet_id_only
          channels: [0]

      - name: trace-me  # dashes work too
        enabled: false
        settings:
          keywords: ["trace"]
          # Optional: an allowlist of channels.
          # channels: [0]
    """
)


def plugins(text):
    """The plugins Meshgram would run from ``text``: name -> (enabled, settings)."""
    settings = build_settings(yaml.safe_load(text))
    return {plugin.name.replace("-", "_"): (plugin.enabled, plugin.settings) for plugin in settings.plugins}


class ExportConfigTests(unittest.TestCase):
    def test_without_changes_it_is_the_file_as_it_is(self):
        result = export_config(CONFIG, {})
        self.assertEqual(result.text, CONFIG)
        self.assertEqual((result.diff, result.changes, result.problems), ("", [], []))

    def test_changed_settings_replace_the_plugins_and_keep_the_comments(self):
        result = export_config(CONFIG, {
            "ping_pong": {"settings": {"keyword_responses": {"Ping": "Pong!"}, "response_dedupe_mode": "packet_id_only", "channels": [0, 2]}},
        })
        self.assertEqual(result.problems, [])
        self.assertEqual(plugins(result.text)["ping_pong"], (True, {"keyword_responses": {"Ping": "Pong!"}, "response_dedupe_mode": "packet_id_only", "channels": [0, 2]}))
        self.assertEqual([(change.name, change.enabled, change.settings) for change in result.changes], [("ping_pong", None, True)])
        # Only what changed: the comment after the removed key stays, quoting and flow style too.
        self.assertIn('        Ping: "Pong!"\n      # Introduces the dedupe mode.\n      response_dedupe_mode', result.text)
        self.assertIn("      channels: [0, 2]\n", result.text)
        self.assertIn("# Meshgram settings.", result.text)
        self.assertIn("- name: trace-me  # dashes work too", result.text)
        self.assertEqual(sorted(line for line in result.diff.splitlines() if line[:1] in "+-" and line[:3] not in ("---", "+++")), [
            '+        Ping: "Pong!"',
            "+      channels: [0, 2]",
            '-        Ack: "Ack"',
            '-        Ping: "Pong"',
            "-      channels: [0]",
        ])

    def test_on_off_state(self):
        result = export_config(CONFIG, {"trace_me": {"enabled": True}, "ping_pong": {"enabled": False}})
        self.assertEqual(result.problems, [])
        self.assertEqual(plugins(result.text)["trace_me"][0], True)
        self.assertEqual(plugins(result.text)["ping_pong"][0], False)
        # ping_pong had no "enabled" (on by default): it's added under its name.
        self.assertIn("  - name: ping_pong\n    enabled: false\n    settings:", result.text)
        self.assertEqual(
            {change.name: change.enabled for change in result.changes}, {"trace_me": True, "ping_pong": False}
        )

    def test_a_new_key_goes_next_to_the_one_before_it(self):
        result = export_config(CONFIG, {"trace_me": {"settings": {"keywords": ["trace"], "response_channel": "same"}}})
        self.assertEqual(result.problems, [])
        # Before the comments that followed "keywords", not after them.
        self.assertIn(
            '      keywords: ["trace"]\n      response_channel: same\n      # Optional: an allowlist of channels.\n',
            result.text,
        )

    def test_a_plugin_config_yaml_does_not_list_is_added(self):
        result = export_config(CONFIG, {"packet_map": {"enabled": True, "settings": {"max_messages": 50}}})
        self.assertEqual(result.problems, [])
        self.assertEqual(plugins(result.text)["packet_map"], (True, {"max_messages": 50}))
        # After the comments of the plugin before it, a blank line apart like the others.
        self.assertTrue(result.text.endswith(
            "      # channels: [0]\n\n  - name: packet_map\n    enabled: true\n    settings:\n      max_messages: 50\n"
        ))

        # Settings only: it stays off, which config.yaml has to say outright.
        result = export_config(CONFIG, {"packet_map": {"settings": {"max_messages": 50}}})
        self.assertEqual(plugins(result.text)["packet_map"], (False, {"max_messages": 50}))

    def test_without_a_plugins_list_the_defaults_are_written_out(self):
        config = 'telegram:\n  bot_token: "123:abc"\n  group_id: -1\n'
        result = export_config(config, {"trace_me": {"enabled": True}})
        self.assertEqual(result.problems, [])
        self.assertEqual(
            plugins(result.text),
            {"bridge": (True, {}), "ping_pong": (True, {}), "trace_me": (True, {})},
        )
        self.assertEqual([change.name for change in result.changes], ["trace_me"])

    def test_strings_pyyaml_would_misread_are_quoted(self):
        settings = {"keyword_responses": {"on": "yes", "Time": "12:30", "Hex": "0x1f", "Empty": "", "Lines": "a\nb"}, "channels": [0]}
        result = export_config(CONFIG, {"ping_pong": {"settings": settings}})
        self.assertEqual(result.problems, [])
        self.assertEqual(plugins(result.text)["ping_pong"], (True, settings))

    def test_secrets_are_written_as_they_are(self):
        result = export_config(CONFIG, {"meshmapper": {"settings": {"iata": "ZRH", "subscribe_password": "s3cret"}}})
        self.assertEqual(plugins(result.text)["meshmapper"][1]["subscribe_password"], "s3cret")
        self.assertIn('bot_token: "123:abc"', result.text)

    def test_changes_config_yaml_already_has_give_the_file_as_it_is(self):
        first = export_config(CONFIG, {"trace_me": {"enabled": True}})
        again = export_config(first.text, {"trace_me": {"enabled": True}})
        self.assertEqual(again.text, first.text)
        self.assertEqual((again.diff, again.changes, again.problems), ("", [], []))

    def test_a_config_meshgram_cannot_use_is_refused(self):
        with self.assertRaisesRegex(ConfigExportError, "isn't valid YAML"):
            export_config("telegram: [", {})
        with self.assertRaisesRegex(ConfigExportError, "bot_token is required"):
            export_config("plugins: []\n", {"trace_me": {"enabled": True}})

    def test_every_example_plugin_survives_a_round_trip(self):
        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        text = example.read_text(encoding="utf-8")
        original = plugins(text)
        changed = {
            name: {"enabled": not enabled, "settings": {**settings, "extra": ["on", 1, {"no": None}]}}
            for name, (enabled, settings) in original.items()
        }
        result = export_config(text, changed)
        self.assertEqual(result.problems, [])
        self.assertEqual(plugins(result.text), {name: (o["enabled"], o["settings"]) for name, o in changed.items()})
        # Everything else is untouched.
        self.assertEqual(text.count("#"), result.text.count("#"))


if __name__ == "__main__":
    unittest.main()

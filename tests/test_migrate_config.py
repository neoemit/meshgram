import io
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from meshgram.config import LEGACY_ENV_VARS, load_settings
from meshgram.migrate_config import ENV_SETTINGS, MigrationError, _parse_args, main, run


CONFIG = """\
# Header comment.
runtime:
  log_level: INFO

mesh:
  backend: meshtastic   # or "meshcore"

meshtastic:
  bridge_channel: 1
  connection:
    mode: serial
    tcp_port: 4403

# Only used when mesh.backend = meshcore.
meshcore:
  connection:
    mode: serial               # serial | tcp | ble
    tcp_port: 5000

telegram:
  include_captions: true

plugins:
  - name: bridge
    enabled: true
    settings:
      channel: 1

  - name: packet-map
    enabled: true
    settings:
      # Prefer PACKET_MAP_PASSWORD in .env.
      host: 127.0.0.1
      port: 8080

  - name: dm_http_command
    enabled: false
    settings:
      commands:
        BATTERY:
          url: "http://${SOLAR_HOST}/battery/"
          auth:
            type: bearer
            token_env: SOLAR_TOKEN  # the bearer token
          headers:
            X-Api-Key: "${SOLAR_API_KEY}"
            X-Other: "${NOT_SET}"
"""

ENV = """\
TELEGRAM_BOT_TOKEN=123456789:ABCDEF
TELEGRAM_GROUP_ID=-1001234567890
MESHGRAM_DATA_DIR=/srv/meshgram
LOG_LEVEL=INFO
MESH_BACKEND=meshcore
MESH_MODE=ble
MESH_PORT=5001
MESH_BLE_ADDRESS=12:34:56:78:90:AB
MESH_BLE_PIN=012345
PACKET_MAP_HOST=0.0.0.0
PACKET_MAP_PASSWORD=on
MESHMAPPER_IATA=YOW
SOLAR_HOST=192.168.0.10
SOLAR_TOKEN=12:30
SOLAR_API_KEY=key
COMPOSE_PROJECT_NAME=meshgram
"""


class MigrateConfigTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.dir = Path(tempdir.name)
        self.config = self.dir / "config.yaml"
        self.env_file = self.dir / ".env"
        self.output = self.dir / "config.migrated.yaml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.env_file.write_text(ENV, encoding="utf-8")

    def _run(self, *extra: str, environ=None) -> tuple[int, str]:
        args = _parse_args(
            ["--config", str(self.config), "--env-file", str(self.env_file), "-o", str(self.output), *extra]
        )
        out = io.StringIO()
        status = run(args, environ=environ or {}, out=out)
        return status, out.getvalue()

    def test_moves_env_settings_into_the_config(self):
        status, report = self._run()
        self.assertEqual(status, 0, report)

        settings = load_settings(str(self.output))
        self.assertEqual(settings.telegram_bot_token, "123456789:ABCDEF")
        self.assertEqual(settings.telegram_group_id, -1001234567890)
        self.assertEqual(settings.meshcore.connection.mode, "ble")
        self.assertEqual(settings.meshcore.connection.tcp_port, 5001)
        self.assertEqual(settings.meshcore.connection.ble_address, "12:34:56:78:90:AB")
        self.assertEqual(settings.meshcore.connection.ble_pin, "012345")
        # The MESH_* connection variables go to the MeshCore radio.
        self.assertIn("    tcp_port: 4403\n", self.output.read_text(encoding="utf-8"))

        packet_map = settings.plugins[1].settings
        self.assertEqual(packet_map["host"], "0.0.0.0")
        self.assertEqual(packet_map["password"], "on")

        command = settings.plugins[2].settings["commands"]["BATTERY"]
        self.assertEqual(command["url"], "http://192.168.0.10/battery/")
        self.assertEqual(command["auth"], {"type": "bearer", "token": "12:30"})
        self.assertEqual(command["headers"], {"X-Api-Key": "key", "X-Other": "${NOT_SET}"})

        self.assertIn("MESHMAPPER_IATA", report)
        self.assertIn("no meshmapper plugin", report)
        self.assertIn("${NOT_SET} isn't set", report)
        self.assertIn("MESHGRAM_DATA_DIR", report)
        self.assertIn("COMPOSE_PROJECT_NAME", report)
        self.assertIn('"meshtastic" -> "meshcore"', report)
        self.assertNotIn("123456789:ABCDEF", report)
        self.assertNotIn("key", report.split("Not moved:")[0].replace("X-Api-Key", ""))

    def test_keeps_comments_and_layout(self):
        self._run()
        text = self.output.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("# Header comment.\nruntime:\n  log_level: INFO\n"))
        self.assertIn("  backend: meshcore     # or \"meshcore\"\n", text)
        self.assertIn("# Only used when mesh.backend = meshcore.\nmeshcore:\n", text)
        self.assertIn("    mode: ble                  # serial | tcp | ble\n", text)
        self.assertIn("telegram:\n  bot_token: 123456789:ABCDEF\n  group_id: -1001234567890\n  include_captions: true\n", text)
        self.assertIn("  - name: bridge\n    enabled: true\n", text)
        self.assertRegex(text, r'\n            token: "12:30" +# the bearer token\n')
        self.assertNotIn("token_env", text)

    def test_quotes_values_yaml_would_misread(self):
        self._run()
        data = yaml.safe_load(self.output.read_text(encoding="utf-8"))
        self.assertEqual(data["meshcore"]["connection"]["ble_pin"], "012345")
        self.assertEqual(data["plugins"][1]["settings"]["password"], "on")
        self.assertEqual(data["plugins"][2]["settings"]["commands"]["BATTERY"]["auth"]["token"], "12:30")

    def test_output_is_private_and_not_overwritten(self):
        self._run()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

        with self.assertRaisesRegex(MigrationError, "already exists"):
            self._run()

        self.output.write_text("old", encoding="utf-8")
        self.output.chmod(0o644)
        status, _ = self._run("--force")
        self.assertEqual(status, 0)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertIn("bot_token", self.output.read_text(encoding="utf-8"))

    def test_environment_wins_over_env_file(self):
        self._run(environ={"MESH_BLE_ADDRESS": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(load_settings(str(self.output)).meshcore.connection.ble_address, "AA:BB:CC:DD:EE:FF")

        self.output.unlink()
        self._run("--ignore-environment", environ={"MESH_BLE_ADDRESS": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(load_settings(str(self.output)).meshcore.connection.ble_address, "12:34:56:78:90:AB")

    def test_running_again_changes_nothing(self):
        self._run()
        first = self.output.read_text(encoding="utf-8")
        self.config.write_text(first, encoding="utf-8")
        self.output.unlink()
        self._run()
        self.assertEqual(self.output.read_text(encoding="utf-8"), first)

    def test_creates_missing_sections(self):
        self.config.write_text("plugins:\n- name: meshmapper\n  settings:\n    iata: XXX\n", encoding="utf-8")
        status, report = self._run()
        self.assertEqual(status, 0, report)
        text = self.output.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("telegram:\n  bot_token:"), text)
        self.assertIn("- name: meshmapper\n  settings:\n    iata: YOW\n", text)
        self.assertEqual(load_settings(str(self.output)).meshcore.connection.ble_pin, "012345")

    def test_writes_to_stdout(self):
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            status, _ = self._run_with_output("-")
        self.assertEqual(status, 0)
        self.assertIn("bot_token: 123456789:ABCDEF", stdout.getvalue())
        self.assertFalse(self.output.exists())

    def _run_with_output(self, output: str) -> tuple[int, str]:
        args = _parse_args(["--config", str(self.config), "--env-file", str(self.env_file), "-o", output])
        out = io.StringIO()
        return run(args, environ={}, out=out), out.getvalue()

    def test_invalid_values_abort(self):
        self.env_file.write_text("MESH_PORT=abc\n", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "MESH_PORT"):
            self._run()
        self.env_file.write_text("MESH_MODE=lora\n", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "MESH_MODE must be one of"):
            self._run()
        self.env_file.write_text("MESH_BACKEND=meshtastic\n", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "Meshtastic support was removed"):
            self._run()
        self.assertFalse(self.output.exists())

    def test_meshtastic_only_variables_are_noted(self):
        self.env_file.write_text(ENV + "MESH_NO_NODES=true\n", encoding="utf-8")
        status, report = self._run()
        self.assertEqual(status, 0, report)
        self.assertIn("MESH_NO_NODES", report.split("Not moved:")[1])
        self.assertNotIn("no_nodes", self.output.read_text(encoding="utf-8"))

    def test_reports_a_config_meshgram_would_refuse(self):
        self.env_file.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")
        status, report = self._run()
        self.assertEqual(status, 1)
        self.assertIn("telegram.bot_token is required", report)

    def test_missing_config_explains_how_to_restore_it(self):
        self.config.unlink()
        with self.assertRaisesRegex(MigrationError, "git show ORIG_HEAD:config.yaml > config.yaml"):
            self._run()

    def test_main_reports_errors(self):
        self.config.unlink()
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            status = main(["--config", str(self.config), "--env-file", str(self.env_file), "-o", str(self.output)])
        self.assertEqual(status, 2)
        self.assertIn("error:", stderr.getvalue())

    def test_every_legacy_variable_is_migrated(self):
        self.assertEqual(set(ENV_SETTINGS), set(LEGACY_ENV_VARS))


if __name__ == "__main__":
    unittest.main()

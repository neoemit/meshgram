import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from meshgram.config import ConfigError, LEGACY_ENV_VARS, WebConfig, legacy_env_vars, load_settings


CREDENTIALS = """
telegram:
  bot_token: "123456789:token"
  group_id: -100123
"""


class ConfigTests(unittest.TestCase):
    def _write(self, body: str) -> str:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / "config.yaml"
        path.write_text(textwrap.dedent(body).strip(), encoding="utf-8")
        return str(path)

    def test_load_settings(self):
        config_path = self._write(
            """
            telegram:
              bot_token: "123456789:token"
              group_id: -100123
              include_captions: false
              sender_prefix_template: "[{display_name}] {message}"
            runtime:
              log_level: debug
            meshcore:
              bridge_channel: 7
              contact_name_overrides:
                "abcd1234": Alpha
              connection:
                mode: tcp
                serial_device: /dev/ttyUSB9
                tcp_host: host.docker.internal
                tcp_port: 5001
            chunking:
              enabled: true
              prefix_template: "({index}/{total}) "
              inter_chunk_delay_ms: 200
              max_chunk_bytes: 140
              broadcast_max_chunk_bytes: 100
              broadcast_min_inter_chunk_delay_ms: 3000
              payload_safety_margin_bytes: 12
              retry_max_attempts: 5
              retry_initial_delay_ms: 250
              retry_backoff_factor: 1.5
              abort_on_chunk_failure: false
            web:
              host: 0.0.0.0
              port: 9090
              password: s3cret
              title: Base camp
            plugins:
              - name: bridge
                enabled: true
                settings:
                  channel: 1
            """
        )

        settings = load_settings(config_path)

        self.assertEqual(settings.telegram_bot_token, "123456789:token")
        self.assertEqual(settings.telegram_group_id, -100123)
        self.assertEqual(settings.config_path, config_path)
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.meshcore.bridge_channel, 7)
        self.assertEqual(settings.meshcore.contact_name_overrides, {"abcd1234": "Alpha"})
        self.assertEqual(settings.meshcore.connection.mode, "tcp")
        self.assertEqual(settings.meshcore.connection.serial_device, "/dev/ttyUSB9")
        self.assertEqual(settings.meshcore.connection.tcp_host, "host.docker.internal")
        self.assertEqual(settings.meshcore.connection.tcp_port, 5001)
        self.assertFalse(settings.telegram.include_captions)
        self.assertEqual(settings.telegram.sender_prefix_template, "[{display_name}] {message}")
        self.assertEqual(settings.chunking.retry_max_attempts, 5)
        self.assertEqual(settings.chunking.retry_initial_delay_ms, 250)
        self.assertEqual(settings.chunking.retry_backoff_factor, 1.5)
        self.assertFalse(settings.chunking.abort_on_chunk_failure)
        self.assertEqual(settings.chunking.max_chunk_bytes, 140)
        self.assertEqual(settings.chunking.broadcast_max_chunk_bytes, 100)
        self.assertEqual(settings.chunking.broadcast_min_inter_chunk_delay_ms, 3000)
        self.assertEqual(settings.chunking.payload_safety_margin_bytes, 12)
        self.assertEqual(
            (settings.web.host, settings.web.port, settings.web.password, settings.web.title),
            ("0.0.0.0", 9090, "s3cret", "Base camp"),
        )
        self.assertEqual(settings.plugins[0].settings["channel"], 1)

    def test_environment_variables_do_not_override_the_file(self):
        config_path = self._write(
            CREDENTIALS
            + """
runtime:
  log_level: INFO
meshcore:
  connection:
    mode: serial
    serial_device: /dev/ttyACM0
"""
        )
        env = {name: "ignored" for name in LEGACY_ENV_VARS}
        env.update({"MESH_BACKEND": "meshtastic", "MESH_MODE": "tcp", "MESH_PORT": "1", "LOG_LEVEL": "DEBUG"})
        with patch.dict(os.environ, env):
            settings = load_settings(config_path)

        self.assertEqual(settings.telegram_bot_token, "123456789:token")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(settings.meshcore.connection.mode, "serial")
        self.assertEqual(settings.meshcore.connection.serial_device, "/dev/ttyACM0")
        self.assertEqual(settings.meshcore.connection.tcp_port, 5000)

    def test_config_path_comes_from_environment(self):
        config_path = self._write(CREDENTIALS)
        with patch.dict(os.environ, {"MESHGRAM_CONFIG_PATH": config_path}):
            settings = load_settings()
        self.assertEqual(settings.config_path, config_path)

    def test_meshcore_with_ble(self):
        config_path = self._write(
            CREDENTIALS
            + """
mesh:
  backend: meshcore
meshcore:
  bridge_channel: 2
  outbound_echo_text_fallback_enabled: true
  outbound_echo_text_fallback_ttl_seconds: 4.5
  connection:
    mode: ble
    ble_address: "12:34:56:78:90:AB"
    ble_pin: "123456"
"""
        )
        settings = load_settings(config_path)

        self.assertEqual(settings.meshcore.bridge_channel, 2)
        self.assertTrue(settings.meshcore.outbound_echo_text_fallback_enabled)
        self.assertEqual(settings.meshcore.outbound_echo_text_fallback_ttl_seconds, 4.5)
        self.assertEqual(settings.meshcore.connection.mode, "ble")
        self.assertEqual(settings.meshcore.connection.ble_address, "12:34:56:78:90:AB")
        self.assertEqual(settings.meshcore.connection.ble_pin, "123456")

    def test_unknown_connection_mode_raises(self):
        config_path = self._write(CREDENTIALS + "\nmeshcore:\n  connection:\n    mode: lora\n")
        with self.assertRaisesRegex(ConfigError, "meshcore.connection.mode"):
            load_settings(config_path)

    def test_meshtastic_is_refused_with_a_hint(self):
        for body in ("\nmesh:\n  backend: meshtastic\n", "\nmeshtastic:\n  bridge_channel: 1\n"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ConfigError, "Meshtastic support was removed"):
                    load_settings(self._write(CREDENTIALS + body))
        # A leftover meshtastic section next to a meshcore one is ignored.
        settings = load_settings(self._write(CREDENTIALS + "\nmeshtastic: {}\nmeshcore:\n  bridge_channel: 3\n"))
        self.assertEqual(settings.meshcore.bridge_channel, 3)

    def test_credentials_are_required(self):
        with self.assertRaisesRegex(ConfigError, "telegram.bot_token"):
            load_settings(self._write("telegram:\n  group_id: -100123\n"))
        with self.assertRaisesRegex(ConfigError, "telegram.group_id is required"):
            load_settings(self._write("telegram:\n  bot_token: token\n"))
        with self.assertRaisesRegex(ConfigError, "telegram.group_id must be an integer"):
            load_settings(self._write("telegram:\n  bot_token: token\n  group_id: my-group\n"))

    def test_missing_config_file_points_to_example_and_migration(self):
        with self.assertRaises(ConfigError) as caught:
            load_settings("/tmp/non-existent-meshgram-config.yaml")
        self.assertIn("config.example.yaml", str(caught.exception))
        self.assertIn("meshgram.migrate_config", str(caught.exception))

    def test_default_plugins_when_none_configured(self):
        settings = load_settings(self._write(CREDENTIALS))
        self.assertEqual([plugin.name for plugin in settings.plugins], ["bridge", "ping_pong"])

    def test_web_defaults(self):
        web = load_settings(self._write(CREDENTIALS)).web
        self.assertEqual((web.enabled, web.host, web.port, web.password, web.title), (True, "127.0.0.1", 8080, "", "Meshgram"))

    def test_web_falls_back_to_old_packet_map_settings(self):
        body = CREDENTIALS + """
plugins:
  - name: packet-map
    settings:
      host: 0.0.0.0
      port: 8081
      password: pw
      tile_url: https://tiles.example/{z}/{x}/{y}.png
      max_packets: 50
"""
        web = load_settings(self._write(body)).web
        self.assertEqual((web.host, web.port, web.password, web.tile_url), ("0.0.0.0", 8081, "pw", "https://tiles.example/{z}/{x}/{y}.png"))
        # A web section wins outright.
        web = load_settings(self._write(body + "web:\n  port: 9000\n")).web
        self.assertEqual((web.host, web.port, web.password), ("127.0.0.1", 9000, ""))

    def test_changes_need_a_password_unless_only_local(self):
        self.assertTrue(WebConfig(host="127.0.0.1").allows_changes)
        self.assertTrue(WebConfig(host="localhost").allows_changes)
        self.assertTrue(WebConfig(host="::1").allows_changes)
        self.assertFalse(WebConfig(host="0.0.0.0").allows_changes)
        self.assertFalse(WebConfig(host="192.168.1.5").allows_changes)
        self.assertTrue(WebConfig(host="0.0.0.0", password="pw").allows_changes)

    def test_legacy_env_vars(self):
        self.assertEqual(legacy_env_vars({"MESH_MODE": "tcp", "PATH": "/bin", "MESHGRAM_DATA_DIR": "/data"}), ["MESH_MODE"])

    def test_example_config_loads(self):
        settings = load_settings(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
        self.assertEqual(settings.meshcore.connection.mode, "serial")
        self.assertTrue(settings.web.enabled)


if __name__ == "__main__":
    unittest.main()

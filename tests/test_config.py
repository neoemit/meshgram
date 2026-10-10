import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from meshgram.config import ConfigError, LEGACY_ENV_VARS, legacy_env_vars, load_settings


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
            meshtastic:
              bridge_channel: 7
              node_name_overrides:
                "!abcd1234": Alpha
                "1234": Bravo
              connection:
                mode: tcp
                serial_device: /dev/ttyUSB9
                tcp_host: host.docker.internal
                tcp_port: 4403
                no_nodes: true
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
              wait_for_ack: false
              ack_timeout_ms: 9000
              abort_on_chunk_failure: false
            plugins:
              - name: bridge
                enabled: true
                settings:
                  channel: 1
                  reply_link_ttl_hours: 24
                  reactions_enabled: true
                  missing_target_policy: fallback_message
                  reply_missing_suffix: "(reply target not found)"
                  reaction_missing_notice_template: "(reaction target not found)"
            """
        )

        settings = load_settings(config_path)

        self.assertEqual(settings.telegram_bot_token, "123456789:token")
        self.assertEqual(settings.telegram_group_id, -100123)
        self.assertEqual(settings.config_path, config_path)
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.meshtastic.bridge_channel, 7)
        self.assertEqual(settings.meshtastic.node_name_overrides["!abcd1234"], "Alpha")
        self.assertEqual(settings.meshtastic.node_name_overrides["1234"], "Bravo")
        self.assertEqual(settings.meshtastic.connection.mode, "tcp")
        self.assertEqual(settings.meshtastic.connection.serial_device, "/dev/ttyUSB9")
        self.assertEqual(settings.meshtastic.connection.tcp_host, "host.docker.internal")
        self.assertEqual(settings.meshtastic.connection.tcp_port, 4403)
        self.assertTrue(settings.meshtastic.connection.no_nodes)
        self.assertFalse(settings.telegram.include_captions)
        self.assertEqual(settings.telegram.sender_prefix_template, "[{display_name}] {message}")
        self.assertEqual(settings.chunking.retry_max_attempts, 5)
        self.assertEqual(settings.chunking.retry_initial_delay_ms, 250)
        self.assertEqual(settings.chunking.retry_backoff_factor, 1.5)
        self.assertFalse(settings.chunking.wait_for_ack)
        self.assertEqual(settings.chunking.ack_timeout_ms, 9000)
        self.assertFalse(settings.chunking.abort_on_chunk_failure)
        self.assertEqual(settings.chunking.max_chunk_bytes, 140)
        self.assertEqual(settings.chunking.broadcast_max_chunk_bytes, 100)
        self.assertEqual(settings.chunking.broadcast_min_inter_chunk_delay_ms, 3000)
        self.assertEqual(settings.chunking.payload_safety_margin_bytes, 12)
        self.assertEqual(settings.plugins[0].settings["reactions_enabled"], True)
        self.assertEqual(settings.plugins[0].settings["missing_target_policy"], "fallback_message")

    def test_environment_variables_do_not_override_the_file(self):
        config_path = self._write(
            CREDENTIALS
            + """
runtime:
  log_level: INFO
meshtastic:
  connection:
    mode: serial
    serial_device: /dev/ttyUSB0
"""
        )
        env = {name: "ignored" for name in LEGACY_ENV_VARS}
        env.update({"MESH_BACKEND": "meshcore", "MESH_MODE": "tcp", "MESH_PORT": "1", "LOG_LEVEL": "DEBUG"})
        with patch.dict(os.environ, env):
            settings = load_settings(config_path)

        self.assertEqual(settings.telegram_bot_token, "123456789:token")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(settings.mesh.backend, "meshtastic")
        self.assertEqual(settings.meshtastic.connection.mode, "serial")
        self.assertEqual(settings.meshtastic.connection.serial_device, "/dev/ttyUSB0")
        self.assertEqual(settings.meshtastic.connection.tcp_port, 4403)

    def test_config_path_comes_from_environment(self):
        config_path = self._write(CREDENTIALS)
        with patch.dict(os.environ, {"MESHGRAM_CONFIG_PATH": config_path}):
            settings = load_settings()
        self.assertEqual(settings.config_path, config_path)

    def test_default_backend_is_meshtastic(self):
        settings = load_settings(self._write(CREDENTIALS))
        self.assertEqual(settings.mesh.backend, "meshtastic")

    def test_meshcore_backend_with_ble(self):
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

        self.assertEqual(settings.mesh.backend, "meshcore")
        self.assertEqual(settings.meshcore.bridge_channel, 2)
        self.assertTrue(settings.meshcore.outbound_echo_text_fallback_enabled)
        self.assertEqual(settings.meshcore.outbound_echo_text_fallback_ttl_seconds, 4.5)
        self.assertEqual(settings.meshcore.connection.mode, "ble")
        self.assertEqual(settings.meshcore.connection.ble_address, "12:34:56:78:90:AB")
        self.assertEqual(settings.meshcore.connection.ble_pin, "123456")

    def test_ble_mode_rejected_for_meshtastic_backend(self):
        config_path = self._write(CREDENTIALS + "\nmeshtastic:\n  connection:\n    mode: ble\n")
        with self.assertRaisesRegex(ConfigError, "meshtastic.connection.mode"):
            load_settings(config_path)

    def test_unknown_backend_raises(self):
        config_path = self._write(CREDENTIALS + "\nmesh:\n  backend: spectrum\n")
        with self.assertRaisesRegex(ConfigError, "mesh.backend"):
            load_settings(config_path)

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

    def test_legacy_env_vars(self):
        self.assertEqual(legacy_env_vars({"MESH_MODE": "tcp", "PATH": "/bin", "MESHGRAM_DATA_DIR": "/data"}), ["MESH_MODE"])

    def test_example_config_loads(self):
        settings = load_settings(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
        self.assertEqual(settings.mesh.backend, "meshtastic")


if __name__ == "__main__":
    unittest.main()

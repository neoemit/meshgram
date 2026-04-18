import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from meshgram.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_load_settings_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    runtime:
                      log_level: DEBUG
                    meshtastic:
                      bridge_channel: 7
                      node_name_overrides:
                        "!abcd1234": Alpha
                        "1234": Bravo
                      connection:
                        mode: serial
                        serial_device: /dev/ttyUSB9
                    telegram:
                      include_captions: false
                      sender_prefix_template: "[{display_name}] {message}"
                    chunking:
                      enabled: true
                      prefix_template: "({index}/{total}) "
                      inter_chunk_delay_ms: 200
                      retry_max_attempts: 5
                      retry_initial_delay_ms: 250
                      retry_backoff_factor: 1.5
                      abort_on_chunk_failure: false
                    plugins:
                      - name: bridge
                        enabled: true
                        settings: {}
                    """
                ).strip(),
                encoding="utf-8",
            )

            env = {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_GROUP_ID": "-100123",
                "MESHGRAM_CONFIG_PATH": str(config_path),
                "LOG_LEVEL": "WARNING",
                "MESH_MODE": "tcp",
                "MESH_DEVICE": "/dev/ttyUSB1",
                "MESH_HOST": "host.docker.internal",
                "MESH_PORT": "4403",
                "MESH_NO_NODES": "true",
            }

            with patch.dict(os.environ, env, clear=False):
                settings = load_settings()

            self.assertEqual(settings.telegram_bot_token, "token")
            self.assertEqual(settings.telegram_group_id, -100123)
            self.assertEqual(settings.log_level, "WARNING")
            self.assertEqual(settings.meshtastic.bridge_channel, 7)
            self.assertEqual(settings.meshtastic.node_name_overrides["!abcd1234"], "Alpha")
            self.assertEqual(settings.meshtastic.node_name_overrides["1234"], "Bravo")
            self.assertEqual(settings.meshtastic.connection.mode, "tcp")
            self.assertEqual(settings.meshtastic.connection.serial_device, "/dev/ttyUSB1")
            self.assertEqual(settings.meshtastic.connection.tcp_host, "host.docker.internal")
            self.assertEqual(settings.meshtastic.connection.tcp_port, 4403)
            self.assertTrue(settings.meshtastic.connection.no_nodes)
            self.assertFalse(settings.telegram.include_captions)
            self.assertEqual(settings.telegram.sender_prefix_template, "[{display_name}] {message}")
            self.assertEqual(settings.chunking.retry_max_attempts, 5)
            self.assertEqual(settings.chunking.retry_initial_delay_ms, 250)
            self.assertEqual(settings.chunking.retry_backoff_factor, 1.5)
            self.assertFalse(settings.chunking.abort_on_chunk_failure)

    def test_default_plugins_when_config_missing(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_GROUP_ID": "-100123",
            "MESHGRAM_CONFIG_PATH": "/tmp/non-existent-config.yaml",
        }

        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        plugin_names = [plugin.name for plugin in settings.plugins]
        self.assertEqual(plugin_names, ["bridge", "ping_pong"])


if __name__ == "__main__":
    unittest.main()

import asyncio
import os
import unittest
from unittest.mock import patch

from meshgram.config import MeshgramSettings
from meshgram.plugins.dm_http_command import DirectMessageHttpCommandPlugin
from meshgram.types import MeshtasticTextEvent, PluginContext


class _FakeHttpPlugin(DirectMessageHttpCommandPlugin):
    def __init__(self, settings, payload: bytes):
        super().__init__(settings)
        self.payload = payload
        self.calls = 0
        self.last_url = ""
        self.last_headers = {}

    def _http_get(self, url: str, timeout_seconds: float, headers: dict[str, str]) -> bytes:
        self.calls += 1
        self.last_url = url
        self.last_headers = dict(headers)
        return self.payload


class DirectMessageHttpCommandPluginTests(unittest.TestCase):
    def setUp(self):
        self.settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-100,
            config_path="config.yaml",
            plugins=[],
        )
        self.context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id="!abcd0001",
        )

    def test_dm_command_fetches_json_value_and_replies_to_sender(self):
        plugin = _FakeHttpPlugin(
            {
                "commands": {
                    "BATTERY": {
                        "url": "http://example.local/battery",
                        "type": "json",
                        "value": "data.inv1.soc",
                        "msg": "{value}%",
                    }
                }
            },
            payload=b'{"data":{"inv1":{"name":"asd","soc":99}}}',
        )
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!ABCD0001",
            packet_id=41,
            reply_id=None,
            channel_index=0,
            text="battery?",
            sender_label="node",
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "99%")
        self.assertEqual(actions[0].destination_id, "!f00d0001")
        self.assertEqual(actions[0].channel_index, 0)
        self.assertEqual(actions[0].reply_id, 41)
        self.assertEqual(plugin.calls, 1)

    def test_non_dm_message_is_ignored(self):
        plugin = _FakeHttpPlugin(
            {
                "commands": {
                    "BATTERY": {
                        "url": "http://example.local/battery",
                        "type": "json",
                        "value": "data.inv1.soc",
                        "msg": "{value}%",
                    }
                }
            },
            payload=b'{"data":{"inv1":{"soc":99}}}',
        )
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!ffffffff",
            packet_id=42,
            reply_id=None,
            channel_index=0,
            text="BATTERY",
            sender_label="node",
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))
        self.assertEqual(actions, [])
        self.assertEqual(plugin.calls, 0)

    def test_unknown_command_is_ignored(self):
        plugin = _FakeHttpPlugin(
            {"commands": {"BATTERY": {"url": "http://example.local/battery", "type": "json"}}},
            payload=b'{"value":99}',
        )
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!abcd0001",
            packet_id=43,
            reply_id=None,
            channel_index=0,
            text="TEMP",
            sender_label="node",
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))
        self.assertEqual(actions, [])
        self.assertEqual(plugin.calls, 0)

    def test_multi_word_message_is_ignored(self):
        plugin = _FakeHttpPlugin(
            {"commands": {"BATTERY": {"url": "http://example.local/battery", "type": "json"}}},
            payload=b'{"value":99}',
        )
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!abcd0001",
            packet_id=44,
            reply_id=None,
            channel_index=0,
            text="BATTERY NOW",
            sender_label="node",
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))
        self.assertEqual(actions, [])
        self.assertEqual(plugin.calls, 0)

    def test_failure_uses_error_message(self):
        plugin = DirectMessageHttpCommandPlugin(
            {
                "error_message": "Unable to fetch {command}",
                "commands": {
                    "BATTERY": {
                        "url": "http://example.local/battery",
                        "type": "json",
                        "value": "data.inv1.soc",
                        "msg": "{value}%",
                    }
                },
            }
        )
        plugin._http_get = lambda url, timeout_seconds, headers: b"not-json"
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!abcd0001",
            packet_id=45,
            reply_id=None,
            channel_index=0,
            text="BATTERY",
            sender_label="node",
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "Unable to fetch BATTERY")

    def test_bearer_auth_from_env(self):
        plugin = _FakeHttpPlugin(
            {
                "commands": {
                    "BATTERY": {
                        "url": "http://example.local/battery",
                        "type": "json",
                        "value": "data.inv1.soc",
                        "msg": "{value}%",
                        "auth": {
                            "type": "bearer",
                            "token_env": "SOLAR_TOKEN",
                        },
                    }
                }
            },
            payload=b'{"data":{"inv1":{"soc":77}}}',
        )
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!abcd0001",
            packet_id=46,
            reply_id=None,
            channel_index=0,
            text="BATTERY",
            sender_label="node",
        )
        with patch.dict(os.environ, {"SOLAR_TOKEN": "secret-token"}, clear=False):
            actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "77%")
        self.assertEqual(plugin.last_headers.get("Authorization"), "Bearer secret-token")

    def test_env_template_expansion_for_url_and_headers(self):
        plugin = _FakeHttpPlugin(
            {
                "commands": {
                    "BATTERY": {
                        "url": "http://${SOLAR_HOST}/battery",
                        "type": "json",
                        "value": "value",
                        "msg": "{value}",
                        "headers": {
                            "X-Api-Key": "${SOLAR_API_KEY}",
                        },
                    }
                }
            },
            payload=b'{"value":55}',
        )
        event = MeshtasticTextEvent(
            from_id="!f00d0001",
            to_id="!abcd0001",
            packet_id=47,
            reply_id=None,
            channel_index=0,
            text="BATTERY",
            sender_label="node",
        )
        with patch.dict(
            os.environ,
            {
                "SOLAR_HOST": "192.168.0.10",
                "SOLAR_API_KEY": "api-key-123",
            },
            clear=False,
        ):
            actions = asyncio.run(plugin.on_meshtastic_message(event, self.context))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "55")
        self.assertEqual(plugin.last_url, "http://192.168.0.10/battery")
        self.assertEqual(plugin.last_headers.get("X-Api-Key"), "api-key-123")


if __name__ == "__main__":
    unittest.main()

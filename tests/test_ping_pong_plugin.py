import asyncio
import unittest

from meshgram.config import MeshgramSettings
from meshgram.plugins.ping_pong import PingPongPlugin
from meshgram.types import MeshtasticTextEvent, PluginContext


class PingPongPluginTests(unittest.TestCase):
    def setUp(self):
        self.settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-100,
            config_path="config.yaml",
            plugins=[],
        )
        self.plugin = PingPongPlugin({"response_text": "Pong"})

    def test_case_insensitive_ping_match(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=42,
            reply_id=None,
            channel_index=3,
            text="  ...PiNg!!! ",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, context))
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.text, "Pong")
        self.assertEqual(action.channel_index, 3)
        self.assertEqual(action.reply_id, 42)

    def test_keyword_response_map_matches_case_insensitive_keywords(self):
        plugin = PingPongPlugin(
            {
                "keyword_responses": {
                    "Ping": "Pong",
                    "Ack": "Ack",
                }
            }
        )
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=52,
            reply_id=None,
            channel_index=3,
            text="ping?",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, context))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "Pong")

        ack_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=53,
            reply_id=None,
            channel_index=3,
            text="ACK!!!",
            sender_label="node",
        )
        ack_actions = asyncio.run(plugin.on_meshtastic_message(ack_event, context))
        self.assertEqual(len(ack_actions), 1)
        self.assertEqual(ack_actions[0].text, "Ack")

    def test_substring_does_not_match(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=43,
            reply_id=None,
            channel_index=3,
            text="ping me",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, context))
        self.assertEqual(actions, [])

    def test_requires_packet_id_for_reply_behavior(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=None,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, context))
        self.assertEqual(actions, [])

    def test_channel_allowlist(self):
        plugin = PingPongPlugin({"response_text": "Pong", "channels": [0, 1]})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        blocked_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=44,
            reply_id=None,
            channel_index=2,
            text="ping",
            sender_label="node",
        )
        blocked_actions = asyncio.run(plugin.on_meshtastic_message(blocked_event, context))
        self.assertEqual(blocked_actions, [])

        allowed_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=45,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        allowed_actions = asyncio.run(plugin.on_meshtastic_message(allowed_event, context))
        self.assertEqual(len(allowed_actions), 1)
        self.assertEqual(allowed_actions[0].channel_index, 1)


if __name__ == "__main__":
    unittest.main()

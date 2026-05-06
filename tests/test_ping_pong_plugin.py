import asyncio
import time as _time_module
import unittest
from unittest.mock import patch

_real_monotonic = _time_module.monotonic


def _monotonic_mock(*scheduled_values):
    """Returns a side_effect callable that yields scheduled values then falls back to real time."""
    it = iter(scheduled_values)

    def _call():
        try:
            return next(it)
        except StopIteration:
            return _real_monotonic()

    return _call

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

    def test_duplicate_keyword_within_dedupe_window_is_ignored(self):
        plugin = PingPongPlugin({"response_text": "Pong", "response_dedupe_ttl_seconds": 6})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=46,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        duplicate_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=47,
            reply_id=None,
            channel_index=1,
            text="PING!!!",
            sender_label="node",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_meshtastic_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=103.0):
            duplicate_actions = asyncio.run(plugin.on_meshtastic_message(duplicate_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(duplicate_actions, [])

    def test_duplicate_keyword_after_dedupe_window_is_allowed(self):
        plugin = PingPongPlugin({"response_text": "Pong", "response_dedupe_ttl_seconds": 6})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=48,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        later_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=49,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_meshtastic_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=107.0):
            later_actions = asyncio.run(plugin.on_meshtastic_message(later_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(len(later_actions), 1)

    def test_nearby_node_retry_within_default_ttl_is_suppressed(self):
        # A 0-hop (nearby) sender may re-originate the same ping with a fresh packet_id
        # a few seconds later (Meshtastic retry before relay confirmation). The default
        # 30-second TTL must cover this window so only one Pong is sent.
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshtasticTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=70,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="nearby",
            raw_packet={"from": 0xAABBCCDD},
        )
        retry_event = MeshtasticTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=71,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="nearby",
            raw_packet={"from": 0xAABBCCDD},
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_meshtastic_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=108.0):
            retry_actions = asyncio.run(plugin.on_meshtastic_message(retry_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(retry_actions, [])

    def test_duplicate_keyword_is_deduped_when_sender_identity_changes_representation(self):
        plugin = PingPongPlugin({"response_text": "Pong", "response_dedupe_ttl_seconds": 6})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshtasticTextEvent(
            from_id=None,
            to_id=None,
            packet_id=50,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="Node A",
            raw_packet={"from": 0xAABBCCDD},
        )
        duplicate_event = MeshtasticTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=51,
            reply_id=None,
            channel_index=1,
            text="PING",
            sender_label="Node Alpha",
            raw_packet={"from": 0xAABBCCDD},
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_meshtastic_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=101.0):
            duplicate_actions = asyncio.run(plugin.on_meshtastic_message(duplicate_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(duplicate_actions, [])

    def test_same_packet_id_repropagated_by_another_node_is_suppressed_for_one_hour(self):
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        original_event = MeshtasticTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=80,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="original",
            raw_packet={"from": 0xAABBCCDD, "id": 80},
        )
        repropagated_event = MeshtasticTextEvent(
            from_id="!11223344",
            to_id=None,
            packet_id=80,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="relay",
            raw_packet={"from": 0x11223344, "id": 80},
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            original_actions = asyncio.run(plugin.on_meshtastic_message(original_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=3699.0):
            repropagated_actions = asyncio.run(plugin.on_meshtastic_message(repropagated_event, context))

        self.assertEqual(len(original_actions), 1)
        self.assertEqual(repropagated_actions, [])

    def test_ignores_ping_from_local_node_id(self):
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id="!00b92212",
        )

        event = MeshtasticTextEvent(
            from_id="!00b92212",
            to_id=None,
            packet_id=52,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="🤖",
            raw_packet={"from": 0x00B92212},
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, context))
        self.assertEqual(actions, [])

    def test_duplicate_keyword_from_same_sender_on_different_channels_is_deduped(self):
        plugin = PingPongPlugin({"response_text": "Pong", "response_dedupe_ttl_seconds": 6, "channels": [0, 1]})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id=None,
        )

        ch0_event = MeshtasticTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=60,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="node",
            raw_packet={"from": 0xAABBCCDD},
        )
        ch1_event = MeshtasticTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=61,
            reply_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
            raw_packet={"from": 0xAABBCCDD},
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            ch0_actions = asyncio.run(plugin.on_meshtastic_message(ch0_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=102.0):
            ch1_actions = asyncio.run(plugin.on_meshtastic_message(ch1_event, context))

        self.assertEqual(len(ch0_actions), 1)
        self.assertEqual(ch1_actions, [])

    def test_ignores_ping_from_local_node_when_only_raw_sender_num_present(self):
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=233,
            local_node_id="00b92212",
        )

        event = MeshtasticTextEvent(
            from_id=None,
            to_id=None,
            packet_id=53,
            reply_id=None,
            channel_index=0,
            text="ping",
            sender_label="🤖",
            raw_packet={"from": 0x00B92212},
        )

        actions = asyncio.run(plugin.on_meshtastic_message(event, context))
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()

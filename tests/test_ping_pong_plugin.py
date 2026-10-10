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
from meshgram.types import MeshTextEvent, PluginContext


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
        event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=42,
            channel_index=3,
            text="  ...PiNg!!! ",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(self.plugin.on_mesh_message(event, context))
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.text, "Pong")
        self.assertEqual(action.channel_index, 3)

    def test_keyword_response_map_matches_case_insensitive_keywords(self):
        plugin = PingPongPlugin(
            {
                "keyword_responses": {
                    "Ping": "Pong",
                    "Ack": "Ack",
                }
            }
        )
        event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=52,
            channel_index=3,
            text="ping?",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(plugin.on_mesh_message(event, context))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "Pong")

        ack_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=53,
            channel_index=3,
            text="ACK!!!",
            sender_label="node",
        )
        ack_actions = asyncio.run(plugin.on_mesh_message(ack_event, context))
        self.assertEqual(len(ack_actions), 1)
        self.assertEqual(ack_actions[0].text, "Ack")

    def test_substring_does_not_match(self):
        event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=43,
            channel_index=3,
            text="ping me",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(self.plugin.on_mesh_message(event, context))
        self.assertEqual(actions, [])

    def test_requires_packet_id_for_reply_behavior(self):
        event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=None,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        actions = asyncio.run(self.plugin.on_mesh_message(event, context))
        self.assertEqual(actions, [])

    def test_channel_allowlist(self):
        plugin = PingPongPlugin({"response_text": "Pong", "channels": [0, 1]})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        blocked_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=44,
            channel_index=2,
            text="ping",
            sender_label="node",
        )
        blocked_actions = asyncio.run(plugin.on_mesh_message(blocked_event, context))
        self.assertEqual(blocked_actions, [])

        allowed_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=45,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        allowed_actions = asyncio.run(plugin.on_mesh_message(allowed_event, context))
        self.assertEqual(len(allowed_actions), 1)
        self.assertEqual(allowed_actions[0].channel_index, 1)

    def test_duplicate_keyword_within_dedupe_window_is_ignored(self):
        plugin = PingPongPlugin(
            {
                "response_text": "Pong",
                "response_dedupe_mode": "sender_keyword_window",
                "response_dedupe_ttl_seconds": 6,
            }
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=46,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        duplicate_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=47,
            channel_index=1,
            text="PING!!!",
            sender_label="node",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_mesh_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=103.0):
            duplicate_actions = asyncio.run(plugin.on_mesh_message(duplicate_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(duplicate_actions, [])

    def test_repeated_keyword_with_new_packet_id_is_allowed_by_default(self):
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=48,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        later_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=49,
            channel_index=1,
            text="ping",
            sender_label="node",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_mesh_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=101.0):
            later_actions = asyncio.run(plugin.on_mesh_message(later_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(len(later_actions), 1)

    def test_duplicate_keyword_after_dedupe_window_is_allowed_when_sender_keyword_mode_enabled(self):
        plugin = PingPongPlugin(
            {
                "response_text": "Pong",
                "response_dedupe_mode": "sender_keyword_window",
                "response_dedupe_ttl_seconds": 6,
            }
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=48,
            channel_index=1,
            text="ping",
            sender_label="node",
        )
        later_event = MeshTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=49,
            channel_index=1,
            text="ping",
            sender_label="node",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_mesh_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=107.0):
            later_actions = asyncio.run(plugin.on_mesh_message(later_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(len(later_actions), 1)

    def test_nearby_node_retry_within_default_ttl_is_suppressed_when_sender_keyword_mode_enabled(self):
        # A 0-hop (nearby) sender may re-originate the same ping with a fresh packet_id
        # a few seconds later (Meshtastic retry before relay confirmation). In
        # sender_keyword_window mode, the default 30-second TTL covers this
        # window so only one Pong is sent.
        plugin = PingPongPlugin(
            {
                "response_text": "Pong",
                "response_dedupe_mode": "sender_keyword_window",
            }
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        first_event = MeshTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=70,
            channel_index=0,
            text="ping",
            sender_label="nearby",
        )
        retry_event = MeshTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=71,
            channel_index=0,
            text="ping",
            sender_label="nearby",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            first_actions = asyncio.run(plugin.on_mesh_message(first_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=108.0):
            retry_actions = asyncio.run(plugin.on_mesh_message(retry_event, context))

        self.assertEqual(len(first_actions), 1)
        self.assertEqual(retry_actions, [])

    def test_same_packet_id_repropagated_by_another_node_is_suppressed_for_one_hour(self):
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        original_event = MeshTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=80,
            channel_index=0,
            text="ping",
            sender_label="original",
        )
        repropagated_event = MeshTextEvent(
            from_id="!11223344",
            to_id=None,
            packet_id=80,
            channel_index=0,
            text="ping",
            sender_label="relay",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            original_actions = asyncio.run(plugin.on_mesh_message(original_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=3699.0):
            repropagated_actions = asyncio.run(plugin.on_mesh_message(repropagated_event, context))

        self.assertEqual(len(original_actions), 1)
        self.assertEqual(repropagated_actions, [])

    def test_ignores_ping_from_local_node_id(self):
        plugin = PingPongPlugin({"response_text": "Pong"})
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id="!00b92212",
        )

        event = MeshTextEvent(
            from_id="!00b92212",
            to_id=None,
            packet_id=52,
            channel_index=0,
            text="ping",
            sender_label="🤖",
        )

        actions = asyncio.run(plugin.on_mesh_message(event, context))
        self.assertEqual(actions, [])

    def test_duplicate_keyword_from_same_sender_on_different_channels_is_deduped(self):
        plugin = PingPongPlugin(
            {
                "response_text": "Pong",
                "response_dedupe_mode": "sender_keyword_window",
                "response_dedupe_ttl_seconds": 6,
                "channels": [0, 1],
            }
        )
        context = PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=233,
            local_node_id=None,
        )

        ch0_event = MeshTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=60,
            channel_index=0,
            text="ping",
            sender_label="node",
        )
        ch1_event = MeshTextEvent(
            from_id="!aabbccdd",
            to_id=None,
            packet_id=61,
            channel_index=1,
            text="ping",
            sender_label="node",
        )

        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=100.0):
            ch0_actions = asyncio.run(plugin.on_mesh_message(ch0_event, context))
        with patch("meshgram.plugins.ping_pong.time.monotonic", return_value=102.0):
            ch1_actions = asyncio.run(plugin.on_mesh_message(ch1_event, context))

        self.assertEqual(len(ch0_actions), 1)
        self.assertEqual(ch1_actions, [])



if __name__ == "__main__":
    unittest.main()

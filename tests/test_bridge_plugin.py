import asyncio
import unittest

from meshgram.config import MeshgramSettings
from meshgram.plugins.bridge import BridgePlugin
from meshgram.settings_schema import validate
from meshgram.text_utils import utf8_len
from meshgram.types import MeshTextEvent, PluginContext, TelegramMessageEvent


def _telegram(text="hello", **overrides) -> TelegramMessageEvent:
    fields = dict(
        chat_id=-999,
        message_id=10,
        text=text,
        text_source="text",
        is_from_bot=False,
        sender_display_name="Alice",
        has_media=False,
    )
    fields.update(overrides)
    return TelegramMessageEvent(**fields)


def _mesh(text="hello", channel_index=2, from_id="aaaa1111bbbb", packet_id="mc-ch-1") -> MeshTextEvent:
    return MeshTextEvent(
        from_id=from_id,
        to_id=None,
        packet_id=packet_id,
        channel_index=channel_index,
        text=text,
        sender_label="Alpha",
    )


class BridgePluginTests(unittest.TestCase):
    def setUp(self):
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-999,
            config_path="config.yaml",
            plugins=[],
        )
        settings.meshcore.bridge_channel = 2
        settings.telegram.include_captions = True
        settings.chunking.enabled = True
        settings.chunking.prefix_template = "({index}/{total}) "
        settings.chunking.inter_chunk_delay_ms = 150
        settings.chunking.max_chunk_bytes = 160
        settings.chunking.broadcast_max_chunk_bytes = 120
        settings.chunking.broadcast_min_inter_chunk_delay_ms = 2500
        settings.chunking.payload_safety_margin_bytes = 16
        self.settings = settings
        self.plugin = BridgePlugin({})

    def _context(self, payload_limit=80, local_node_id=None):
        return PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=payload_limit,
            local_node_id=local_node_id,
        )

    def _send_telegram(self, event, payload_limit=80, plugin=None):
        return asyncio.run((plugin or self.plugin).on_telegram_message(event, self._context(payload_limit=payload_limit)))

    def test_mesh_to_telegram_respects_channel(self):
        actions = asyncio.run(self.plugin.on_mesh_message(_mesh(channel_index=1), self._context()))
        self.assertEqual(actions, [])

    def test_mesh_to_telegram_ignores_local_node(self):
        actions = asyncio.run(self.plugin.on_mesh_message(_mesh(text="loop"), self._context(local_node_id="aaaa1111bbbb")))
        self.assertEqual(actions, [])

    def test_mesh_to_telegram_forward(self):
        actions = asyncio.run(self.plugin.on_mesh_message(_mesh(text="hello mesh"), self._context()))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].chat_id, -999)
        self.assertEqual(actions[0].text, "[Alpha] hello mesh")

    def test_telegram_to_mesh_ignores_bots_and_other_chats(self):
        self.assertEqual(self._send_telegram(_telegram(is_from_bot=True)), [])
        self.assertEqual(self._send_telegram(_telegram(chat_id=-1)), [])
        self.assertEqual(self._send_telegram(_telegram(text="   ")), [])

    def test_telegram_to_mesh_caption_handling(self):
        self.settings.telegram.include_captions = False
        event = _telegram(text="caption text", text_source="caption", has_media=True)
        self.assertEqual(self._send_telegram(event), [])

    def test_telegram_to_mesh_chunks_when_needed(self):
        actions = self._send_telegram(_telegram("this message is definitely long enough to chunk across packets"), payload_limit=28)
        self.assertGreater(len(actions), 1)
        self.assertEqual(actions[0].delay_ms, 0)
        self.assertEqual(actions[1].delay_ms, 2500)
        self.assertEqual(actions[0].retry_max_attempts, 3)
        self.assertEqual(actions[0].retry_initial_delay_ms, 500)
        self.assertEqual(actions[0].retry_backoff_factor, 2.0)
        self.assertTrue(actions[0].abort_on_failure)
        self.assertIsNotNone(actions[0].sequence_id)
        self.assertEqual(actions[0].sequence_index, 1)
        self.assertEqual(actions[0].sequence_total, len(actions))
        self.assertEqual(actions[1].sequence_id, actions[0].sequence_id)
        self.assertTrue(all(action.channel_index == 2 and action.destination_id is None for action in actions))
        # Channel messages are broadcasts: nobody acknowledges them.
        self.assertFalse(any(action.want_ack or action.wait_for_ack for action in actions))

    def test_telegram_chunked_send_enforces_minimum_inter_chunk_delay(self):
        self.settings.chunking.inter_chunk_delay_ms = 10
        self.settings.chunking.broadcast_min_inter_chunk_delay_ms = 0
        actions = self._send_telegram(_telegram("x" * 220), payload_limit=40)
        self.assertGreater(len(actions), 1)
        self.assertEqual(actions[1].delay_ms, BridgePlugin.MIN_CHUNK_DELAY_MS)

    def test_short_message_is_one_unsequenced_action(self):
        actions = self._send_telegram(_telegram("short"), payload_limit=240)
        self.assertEqual(len(actions), 1)
        self.assertIsNone(actions[0].sequence_id)
        self.assertFalse(actions[0].abort_on_failure)

    def test_chunking_reserves_payload_safety_margin(self):
        self.settings.chunking.payload_safety_margin_bytes = 10
        self.settings.chunking.max_chunk_bytes = 0
        actions = self._send_telegram(_telegram("x" * 200), payload_limit=40)
        self.assertGreater(len(actions), 1)
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), 30)

    def test_chunking_caps_payload_to_safe_max_chunk_bytes(self):
        self.settings.chunking.payload_safety_margin_bytes = 0
        self.settings.chunking.max_chunk_bytes = 30
        actions = self._send_telegram(_telegram("x" * 180), payload_limit=200)
        self.assertGreater(len(actions), 1)
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), 30)

    def test_chunking_uses_safe_default_cap_when_configured_cap_is_zero(self):
        self.settings.chunking.payload_safety_margin_bytes = 0
        self.settings.chunking.max_chunk_bytes = 0
        self.settings.chunking.broadcast_max_chunk_bytes = 0
        actions = self._send_telegram(_telegram("x" * 260), payload_limit=300)
        self.assertGreater(len(actions), 1)
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), self.plugin.DEFAULT_SAFE_MAX_CHUNK_BYTES)

    def test_broadcast_cap_limits_chunk_size(self):
        self.settings.chunking.max_chunk_bytes = 180
        self.settings.chunking.broadcast_max_chunk_bytes = 90
        self.settings.chunking.payload_safety_margin_bytes = 0
        actions = self._send_telegram(_telegram("x" * 260), payload_limit=300)
        self.assertGreater(len(actions), 1)
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), 90)

    def test_channel_setting_overrides_meshcore_bridge_channel(self):
        plugin = BridgePlugin({"channel": 1})
        self.assertEqual(asyncio.run(plugin.on_mesh_message(_mesh(channel_index=2), self._context())), [])
        actions = self._send_telegram(_telegram(), plugin=plugin)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].channel_index, 1)

    def test_telegram_sender_display_name_is_compacted_to_first_token(self):
        actions = self._send_telegram(_telegram(sender_display_name="Name Surname"))
        self.assertEqual(actions[0].text, "[Name] hello")
        actions = self._send_telegram(_telegram(sender_display_name="Alice"))
        self.assertEqual(actions[0].text, "[Alice] hello")

    def test_settings_schema(self):
        validate({"channel": 3}, BridgePlugin.settings_schema)
        with self.assertRaises(ValueError):
            validate({"channel": "three"}, BridgePlugin.settings_schema)


if __name__ == "__main__":
    unittest.main()

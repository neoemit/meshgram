import asyncio
import unittest

from meshgram.config import MeshgramSettings
from meshgram.plugins.bridge import BridgePlugin
from meshgram.text_utils import utf8_len
from meshgram.types import (
    MeshtasticReactionEvent,
    MeshtasticTextEvent,
    PluginContext,
    SendMeshtasticReactionAction,
    SendTelegramReactionAction,
    TelegramMessageEvent,
    TelegramReactionEvent,
)


class _FakeReplyLinks:
    def __init__(self):
        self.telegram_to_mesh = {}
        self.mesh_to_telegram = {}

    def get_meshtastic_for_telegram(self, chat_id, telegram_message_id):
        return self.telegram_to_mesh.get((chat_id, telegram_message_id))

    def get_telegram_for_meshtastic(self, chat_id, meshtastic_packet_id):
        value = self.mesh_to_telegram.get(meshtastic_packet_id)
        if value is None:
            return None
        mapped_chat, mapped_message = value
        if mapped_chat != chat_id:
            return None
        return mapped_message


class BridgePluginTests(unittest.TestCase):
    def setUp(self):
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-999,
            config_path="config.yaml",
            plugins=[],
        )
        settings.meshtastic.bridge_channel = 2
        settings.telegram.include_captions = True
        settings.chunking.enabled = True
        settings.chunking.prefix_template = "({index}/{total}) "
        settings.chunking.inter_chunk_delay_ms = 150
        settings.chunking.max_chunk_bytes = 160
        settings.chunking.payload_safety_margin_bytes = 16
        settings.chunking.wait_for_ack = True
        settings.chunking.ack_timeout_ms = 20000
        self.settings = settings

        self.reply_links = _FakeReplyLinks()
        self.plugin = BridgePlugin({})

    def _context(self, payload_limit=80, local_node_id=None):
        return PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            meshtastic_payload_limit=payload_limit,
            local_node_id=local_node_id,
            reply_links=self.reply_links,
        )

    def test_meshtastic_to_telegram_respects_channel(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=1,
            reply_id=None,
            channel_index=1,
            text="hello",
            sender_label="Alpha",
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, self._context()))
        self.assertEqual(actions, [])

    def test_meshtastic_to_telegram_ignores_local_node(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=2,
            reply_id=None,
            channel_index=2,
            text="loop",
            sender_label="Alpha",
        )

        actions = asyncio.run(
            self.plugin.on_meshtastic_message(event, self._context(local_node_id="!aaaa1111"))
        )
        self.assertEqual(actions, [])

    def test_meshtastic_to_telegram_forward(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=3,
            reply_id=None,
            channel_index=2,
            text="hello mesh",
            sender_label="Alpha",
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].chat_id, -999)
        self.assertEqual(actions[0].text, "[Alpha] hello mesh")
        self.assertEqual(actions[0].bridge_source_meshtastic_packet_id, 3)

    def test_meshtastic_reply_maps_to_telegram_reply(self):
        self.reply_links.mesh_to_telegram[1234] = (-999, 88)
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=4,
            reply_id=1234,
            channel_index=2,
            text="reply message",
            sender_label="Alpha",
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, self._context()))
        self.assertEqual(actions[0].reply_to_message_id, 88)

    def test_meshtastic_reply_missing_mapping_appends_suffix(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=44,
            reply_id=7777,
            channel_index=2,
            text="reply message",
            sender_label="Alpha",
        )

        actions = asyncio.run(self.plugin.on_meshtastic_message(event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertIsNone(actions[0].reply_to_message_id)
        self.assertEqual(actions[0].text, "[Alpha] reply message (reply target not found)")

    def test_telegram_to_meshtastic_ignores_bots(self):
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=10,
            reply_to_message_id=None,
            text="hello",
            text_source="text",
            is_from_bot=True,
            sender_display_name="Bot",
            has_media=False,
        )
        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context()))
        self.assertEqual(actions, [])

    def test_telegram_to_meshtastic_caption_handling(self):
        self.settings.telegram.include_captions = False
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=10,
            reply_to_message_id=None,
            text="caption text",
            text_source="caption",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=True,
        )
        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context()))
        self.assertEqual(actions, [])

    def test_telegram_reply_maps_to_meshtastic_reply_id_on_first_chunk(self):
        self.reply_links.telegram_to_mesh[(-999, 55)] = 777
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=10,
            reply_to_message_id=55,
            text="this message is definitely long enough to chunk across packets",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=28)))
        self.assertGreater(len(actions), 1)
        self.assertEqual(actions[0].reply_id, 777)
        self.assertIsNone(actions[1].reply_id)

    def test_telegram_reply_missing_mapping_appends_suffix(self):
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=10,
            reply_to_message_id=55,
            text="hello",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )
        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=80)))
        self.assertEqual(len(actions), 1)
        self.assertIsNone(actions[0].reply_id)
        self.assertEqual(actions[0].text, "[Alice] hello (reply target not found)")

    def test_telegram_to_meshtastic_chunks_when_needed_and_sets_link_metadata(self):
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=10,
            reply_to_message_id=None,
            text="this message is definitely long enough to chunk across packets",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )
        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=28)))
        self.assertGreater(len(actions), 1)
        self.assertEqual(actions[1].delay_ms, 900)
        self.assertEqual(actions[0].retry_max_attempts, 3)
        self.assertEqual(actions[0].retry_initial_delay_ms, 500)
        self.assertEqual(actions[0].retry_backoff_factor, 2.0)
        self.assertTrue(actions[0].wait_for_ack)
        self.assertEqual(actions[0].ack_timeout_ms, 20000)
        self.assertTrue(actions[0].abort_on_failure)
        self.assertTrue(actions[0].want_ack)
        self.assertTrue(actions[0].require_packet_id)
        self.assertIsNotNone(actions[0].sequence_id)
        self.assertEqual(actions[0].sequence_index, 1)
        self.assertEqual(actions[0].sequence_total, len(actions))
        self.assertEqual(actions[1].sequence_id, actions[0].sequence_id)
        self.assertTrue(actions[0].bridge_canonical_for_telegram_message)
        self.assertFalse(actions[1].bridge_canonical_for_telegram_message)
        self.assertEqual(actions[0].bridge_source_telegram_chat_id, -999)
        self.assertEqual(actions[0].bridge_source_telegram_message_id, 10)
        self.assertEqual(actions[0].channel_index, 2)

    def test_telegram_chunked_send_enforces_minimum_inter_chunk_delay(self):
        self.settings.chunking.inter_chunk_delay_ms = 10
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=111,
            reply_to_message_id=None,
            text="x" * 220,
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=40)))
        self.assertGreater(len(actions), 1)
        self.assertEqual(actions[1].delay_ms, 900)

    def test_non_chunked_message_does_not_enable_ack_wait_gate(self):
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=112,
            reply_to_message_id=None,
            text="short",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=240)))
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0].wait_for_ack)
        self.assertEqual(actions[0].ack_timeout_ms, 0)

    def test_chunking_reserves_payload_safety_margin(self):
        self.settings.chunking.payload_safety_margin_bytes = 10
        self.settings.chunking.max_chunk_bytes = 0
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=101,
            reply_to_message_id=None,
            text="x" * 200,
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        payload_limit = 40
        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=payload_limit)))
        self.assertGreater(len(actions), 1)
        reserved_limit = payload_limit - self.settings.chunking.payload_safety_margin_bytes
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), reserved_limit)

    def test_chunking_reserves_extra_margin_for_reply_id(self):
        self.settings.chunking.payload_safety_margin_bytes = 10
        self.settings.chunking.max_chunk_bytes = 0
        self.reply_links.telegram_to_mesh[(-999, 55)] = 777
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=102,
            reply_to_message_id=55,
            text="x" * 200,
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        payload_limit = 40
        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=payload_limit)))
        self.assertGreater(len(actions), 1)
        reserved_limit = payload_limit - self.settings.chunking.payload_safety_margin_bytes - self.plugin.REPLY_ID_EXTRA_MARGIN_BYTES
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), reserved_limit)

    def test_chunking_caps_payload_to_safe_max_chunk_bytes(self):
        self.settings.chunking.payload_safety_margin_bytes = 0
        self.settings.chunking.max_chunk_bytes = 30
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=103,
            reply_to_message_id=None,
            text="x" * 180,
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=200)))
        self.assertGreater(len(actions), 1)
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), 30)

    def test_chunking_uses_safe_default_cap_when_configured_cap_is_zero(self):
        self.settings.chunking.payload_safety_margin_bytes = 0
        self.settings.chunking.max_chunk_bytes = 0
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=104,
            reply_to_message_id=None,
            text="x" * 260,
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=300)))
        self.assertGreater(len(actions), 1)
        for action in actions:
            self.assertLessEqual(utf8_len(action.text), self.plugin.DEFAULT_SAFE_MAX_CHUNK_BYTES)

    def test_bridge_plugin_channel_setting_overrides_global_bridge_channel(self):
        plugin = BridgePlugin({"channel": 1})
        mesh_event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=6,
            reply_id=None,
            channel_index=2,
            text="ignore this",
            sender_label="Alpha",
        )
        actions = asyncio.run(plugin.on_meshtastic_message(mesh_event, self._context()))
        self.assertEqual(actions, [])

        tg_event = TelegramMessageEvent(
            chat_id=-999,
            message_id=99,
            reply_to_message_id=None,
            text="hello",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )
        actions = asyncio.run(plugin.on_telegram_message(tg_event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].channel_index, 1)

    def test_telegram_sender_display_name_is_compacted_to_first_token(self):
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=15,
            reply_to_message_id=None,
            text="hello",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Name Surname",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=80)))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "[Name] hello")

    def test_telegram_sender_single_token_remains_unchanged(self):
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=16,
            reply_to_message_id=None,
            text="hello",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_message(event, self._context(payload_limit=80)))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "[Alice] hello")

    def test_telegram_reaction_mapped_emits_meshtastic_reaction_action(self):
        self.reply_links.telegram_to_mesh[(-999, 77)] = 9001
        event = TelegramReactionEvent(
            chat_id=-999,
            message_id=77,
            emoji="❤",
            is_from_bot=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_reaction(event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], SendMeshtasticReactionAction)
        self.assertEqual(actions[0].target_packet_id, 9001)
        self.assertEqual(actions[0].emoji, "❤")
        self.assertEqual(actions[0].channel_index, 2)
        self.assertTrue(actions[0].want_ack)
        self.assertEqual(actions[0].retry_max_attempts, 3)

    def test_telegram_reaction_missing_mapping_emits_notice_message(self):
        event = TelegramReactionEvent(
            chat_id=-999,
            message_id=77,
            emoji="❤",
            is_from_bot=False,
        )

        actions = asyncio.run(self.plugin.on_telegram_reaction(event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, "(reaction target not found)")
        self.assertEqual(actions[0].channel_index, 2)
        self.assertTrue(actions[0].want_ack)
        self.assertTrue(actions[0].require_packet_id)

    def test_meshtastic_reaction_mapped_emits_telegram_reaction_action(self):
        self.reply_links.mesh_to_telegram[1001] = (-999, 201)
        event = MeshtasticReactionEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=300,
            target_packet_id=1001,
            channel_index=2,
            emoji="❤",
            sender_label="Alpha",
        )

        actions = asyncio.run(self.plugin.on_meshtastic_reaction(event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], SendTelegramReactionAction)
        self.assertEqual(actions[0].chat_id, -999)
        self.assertEqual(actions[0].message_id, 201)
        self.assertEqual(actions[0].emoji, "❤")

    def test_meshtastic_reaction_missing_mapping_emits_notice_message(self):
        event = MeshtasticReactionEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=300,
            target_packet_id=9999,
            channel_index=2,
            emoji="❤",
            sender_label="Alpha",
        )

        actions = asyncio.run(self.plugin.on_meshtastic_reaction(event, self._context()))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].chat_id, -999)
        self.assertEqual(actions[0].text, "(reaction target not found)")

    def test_meshtastic_reaction_ignores_local_node(self):
        event = MeshtasticReactionEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=300,
            target_packet_id=9999,
            channel_index=2,
            emoji="❤",
            sender_label="Alpha",
        )

        actions = asyncio.run(
            self.plugin.on_meshtastic_reaction(event, self._context(local_node_id="!aaaa1111"))
        )
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()

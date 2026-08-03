import asyncio
import unittest

from meshgram.config import MeshgramSettings, PluginConfig
from meshgram.plugins.bridge import BridgePlugin, validate_bridge_channel_mappings
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
        settings.chunking.broadcast_max_chunk_bytes = 120
        settings.chunking.broadcast_min_inter_chunk_delay_ms = 2500
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
            mesh_payload_limit=payload_limit,
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
        self.assertEqual(actions[1].delay_ms, 2500)
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
        self.assertEqual(actions[1].delay_ms, 2500)

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

    def test_broadcast_profile_caps_chunk_size_with_broadcast_limit(self):
        self.settings.chunking.max_chunk_bytes = 180
        self.settings.chunking.broadcast_max_chunk_bytes = 90
        self.settings.chunking.payload_safety_margin_bytes = 0
        event = TelegramMessageEvent(
            chat_id=-999,
            message_id=105,
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
            self.assertLessEqual(utf8_len(action.text), 90)

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


class BridgeChannelMappingTests(unittest.TestCase):
    """1:1 telegram_chat_id <-> mesh channel mapping across multiple BridgePlugin instances.

    Each instance is configured with its own ``telegram_chat_id``/``channel`` pair
    (settings dict), mirroring how two ``- name: bridge`` entries would be declared
    in config.yaml for two independent chat<->channel bridges.
    """

    def setUp(self):
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-999,
            config_path="config.yaml",
            plugins=[],
        )
        settings.meshtastic.bridge_channel = 0
        settings.telegram.include_captions = True
        settings.chunking.enabled = True
        settings.chunking.prefix_template = "({index}/{total}) "
        settings.chunking.inter_chunk_delay_ms = 150
        settings.chunking.max_chunk_bytes = 160
        settings.chunking.broadcast_max_chunk_bytes = 120
        settings.chunking.broadcast_min_inter_chunk_delay_ms = 2500
        settings.chunking.payload_safety_margin_bytes = 16
        settings.chunking.wait_for_ack = True
        settings.chunking.ack_timeout_ms = 20000
        self.settings = settings

        self.reply_links = _FakeReplyLinks()
        self.plugin_a = BridgePlugin({"telegram_chat_id": -111, "channel": 0})
        self.plugin_b = BridgePlugin({"telegram_chat_id": -222, "channel": 3})

    def _context(self, payload_limit=80, local_node_id=None):
        return PluginContext(
            settings=self.settings,
            telegram_group_id=self.settings.telegram_group_id,
            mesh_payload_limit=payload_limit,
            local_node_id=local_node_id,
            reply_links=self.reply_links,
        )

    def test_telegram_chat_id_setting_overrides_context_default(self):
        self.assertEqual(self.plugin_a._telegram_chat_id(self._context()), -111)
        self.assertEqual(self.plugin_b._telegram_chat_id(self._context()), -222)

    def test_bridge_without_telegram_chat_id_setting_falls_back_to_context_default(self):
        plugin = BridgePlugin({})
        self.assertEqual(plugin._telegram_chat_id(self._context()), -999)

    def test_mesh_message_on_channel_0_routes_only_to_chat_a(self):
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=1,
            reply_id=None,
            channel_index=0,
            text="hello from channel 0",
            sender_label="Alpha",
        )

        actions_a = asyncio.run(self.plugin_a.on_meshtastic_message(event, self._context()))
        actions_b = asyncio.run(self.plugin_b.on_meshtastic_message(event, self._context()))

        self.assertEqual(len(actions_a), 1)
        self.assertEqual(actions_a[0].chat_id, -111)
        self.assertEqual(actions_b, [])

    def test_mesh_message_on_channel_3_routes_only_to_chat_b(self):
        event = MeshtasticTextEvent(
            from_id="!bbbb2222",
            to_id=None,
            packet_id=2,
            reply_id=None,
            channel_index=3,
            text="hello from channel 3",
            sender_label="Beta",
        )

        actions_a = asyncio.run(self.plugin_a.on_meshtastic_message(event, self._context()))
        actions_b = asyncio.run(self.plugin_b.on_meshtastic_message(event, self._context()))

        self.assertEqual(actions_a, [])
        self.assertEqual(len(actions_b), 1)
        self.assertEqual(actions_b[0].chat_id, -222)

    def test_telegram_message_from_chat_a_routes_only_to_channel_0(self):
        event = TelegramMessageEvent(
            chat_id=-111,
            message_id=10,
            reply_to_message_id=None,
            text="hello",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        actions_a = asyncio.run(self.plugin_a.on_telegram_message(event, self._context()))
        actions_b = asyncio.run(self.plugin_b.on_telegram_message(event, self._context()))

        self.assertEqual(len(actions_a), 1)
        self.assertEqual(actions_a[0].channel_index, 0)
        self.assertEqual(actions_b, [])

    def test_telegram_message_from_chat_b_routes_only_to_channel_3(self):
        event = TelegramMessageEvent(
            chat_id=-222,
            message_id=11,
            reply_to_message_id=None,
            text="hello",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Bob",
            has_media=False,
        )

        actions_a = asyncio.run(self.plugin_a.on_telegram_message(event, self._context()))
        actions_b = asyncio.run(self.plugin_b.on_telegram_message(event, self._context()))

        self.assertEqual(actions_a, [])
        self.assertEqual(len(actions_b), 1)
        self.assertEqual(actions_b[0].channel_index, 3)

    def test_reply_link_registered_for_own_chat_resolves_via_plugin_specific_chat_id(self):
        # Link belongs to plugin B's own chat (-222), NOT the global
        # settings.telegram_group_id (-999). Only a per-instance chat_id lookup
        # (not the global default) can find it.
        self.reply_links.mesh_to_telegram[100] = (-222, 10)

        event = MeshtasticTextEvent(
            from_id="!bbbb2222",
            to_id=None,
            packet_id=5,
            reply_id=100,
            channel_index=3,
            text="reply on channel 3",
            sender_label="Beta",
        )

        actions_b = asyncio.run(self.plugin_b.on_meshtastic_message(event, self._context()))
        self.assertEqual(len(actions_b), 1)
        self.assertEqual(actions_b[0].reply_to_message_id, 10)

    def test_reply_link_registered_for_other_mapping_does_not_leak(self):
        # Link belongs to plugin A's chat (-111); plugin B must not resolve it
        # even though both instances share the same reply_links registry.
        self.reply_links.mesh_to_telegram[100] = (-111, 10)

        event = MeshtasticTextEvent(
            from_id="!bbbb2222",
            to_id=None,
            packet_id=6,
            reply_id=100,
            channel_index=3,
            text="reply on channel 3",
            sender_label="Beta",
        )

        actions_b = asyncio.run(self.plugin_b.on_meshtastic_message(event, self._context()))
        self.assertEqual(len(actions_b), 1)
        self.assertIsNone(actions_b[0].reply_to_message_id)
        self.assertIn("(reply target not found)", actions_b[0].text)

    def test_telegram_reaction_routes_by_owning_chat(self):
        self.reply_links.telegram_to_mesh[(-111, 77)] = 9001
        event_chat_a = TelegramReactionEvent(chat_id=-111, message_id=77, emoji="❤", is_from_bot=False)
        event_chat_b = TelegramReactionEvent(chat_id=-222, message_id=77, emoji="❤", is_from_bot=False)

        actions_a = asyncio.run(self.plugin_a.on_telegram_reaction(event_chat_a, self._context()))
        actions_b_wrong_chat = asyncio.run(self.plugin_b.on_telegram_reaction(event_chat_a, self._context()))
        actions_b_own_chat = asyncio.run(self.plugin_b.on_telegram_reaction(event_chat_b, self._context()))

        self.assertEqual(len(actions_a), 1)
        self.assertEqual(actions_a[0].channel_index, 0)
        self.assertEqual(actions_b_wrong_chat, [])
        self.assertEqual(len(actions_b_own_chat), 1)
        self.assertEqual(actions_b_own_chat[0].channel_index, 3)

    def test_mesh_reaction_routes_to_owning_chat_only(self):
        self.reply_links.mesh_to_telegram[1001] = (-222, 201)
        event = MeshtasticReactionEvent(
            from_id="!bbbb2222",
            to_id=None,
            packet_id=300,
            target_packet_id=1001,
            channel_index=3,
            emoji="❤",
            sender_label="Beta",
        )

        actions_a = asyncio.run(self.plugin_a.on_meshtastic_reaction(event, self._context()))
        actions_b = asyncio.run(self.plugin_b.on_meshtastic_reaction(event, self._context()))

        self.assertEqual(actions_a, [])
        self.assertEqual(len(actions_b), 1)
        self.assertEqual(actions_b[0].chat_id, -222)
        self.assertEqual(actions_b[0].message_id, 201)


class BridgeChannelMappingValidationTests(unittest.TestCase):
    """Fail-fast startup validation: bridge instances must form a strict 1:1
    telegram_chat_id <-> channel mapping (no chat or channel reused)."""

    def _settings(self, plugins):
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-999,
            config_path="config.yaml",
            plugins=plugins,
        )
        settings.meshtastic.bridge_channel = 0
        return settings

    def test_valid_1_to_1_mapping_passes(self):
        plugins = [
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -111, "channel": 0}),
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -222, "channel": 3}),
        ]
        settings = self._settings(plugins)
        validate_bridge_channel_mappings(settings.plugins, settings)  # must not raise

    def test_duplicate_chat_id_raises(self):
        plugins = [
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -111, "channel": 0}),
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -111, "channel": 3}),
        ]
        settings = self._settings(plugins)
        with self.assertRaises(ValueError) as ctx:
            validate_bridge_channel_mappings(settings.plugins, settings)
        self.assertIn("telegram_chat_id", str(ctx.exception))

    def test_duplicate_channel_raises(self):
        plugins = [
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -111, "channel": 0}),
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -222, "channel": 0}),
        ]
        settings = self._settings(plugins)
        with self.assertRaises(ValueError) as ctx:
            validate_bridge_channel_mappings(settings.plugins, settings)
        self.assertIn("channel", str(ctx.exception))

    def test_disabled_bridge_plugin_ignored_in_validation(self):
        plugins = [
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -111, "channel": 0}),
            PluginConfig(name="bridge", enabled=False, settings={"telegram_chat_id": -111, "channel": 0}),
        ]
        settings = self._settings(plugins)
        validate_bridge_channel_mappings(settings.plugins, settings)  # disabled entry must not collide

    def test_single_default_bridge_without_overrides_passes(self):
        # Back-compat: today's typical single-bridge config with no
        # telegram_chat_id/channel override must still validate cleanly.
        plugins = [PluginConfig(name="bridge", enabled=True, settings={})]
        settings = self._settings(plugins)
        validate_bridge_channel_mappings(settings.plugins, settings)

    def test_non_bridge_plugins_are_ignored(self):
        plugins = [
            PluginConfig(name="bridge", enabled=True, settings={"telegram_chat_id": -111, "channel": 0}),
            PluginConfig(name="ping_pong", enabled=True, settings={}),
        ]
        settings = self._settings(plugins)
        validate_bridge_channel_mappings(settings.plugins, settings)  # must not raise


if __name__ == "__main__":
    unittest.main()

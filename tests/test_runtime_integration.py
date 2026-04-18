import asyncio
import unittest

from meshgram.app import MeshgramApp
from meshgram.config import MeshgramSettings, PluginConfig
from meshgram.types import MeshtasticTextEvent, SendMeshtasticAction, TelegramMessageEvent


class _FakeTelegramMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.messages = []
        self._next_message_id = 100

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        self._next_message_id += 1
        return _FakeTelegramMessage(self._next_message_id)


class _FakeTelegramApp:
    def __init__(self):
        self.bot = _FakeBot()


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-555,
            config_path="config.yaml",
            plugins=[
                PluginConfig(name="bridge", enabled=True, settings={"reply_link_ttl_hours": 24}),
                PluginConfig(name="ping_pong", enabled=True, settings={}),
            ],
        )
        settings.meshtastic.bridge_channel = 0
        settings.chunking.enabled = True
        settings.chunking.prefix_template = "({index}/{total}) "
        settings.chunking.inter_chunk_delay_ms = 0

        self.app = MeshgramApp(settings)
        self.app.bot_app = _FakeTelegramApp()
        self.app.meshtastic.iface = object()

        self.sent_mesh = []

        def _fake_send_mesh(action):
            self.sent_mesh.append(action)
            return {"id": 1000 + len(self.sent_mesh)}

        self.app.meshtastic.send_text = _fake_send_mesh

    def test_telegram_to_meshtastic_dispatch(self):
        event = TelegramMessageEvent(
            chat_id=-555,
            message_id=1,
            reply_to_message_id=None,
            text="hello from telegram",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        asyncio.run(self.app._dispatch_telegram_message(event))

        self.assertEqual(len(self.sent_mesh), 1)
        self.assertIn("[Alice] hello from telegram", self.sent_mesh[0].text)

    def test_meshtastic_to_telegram_loop_prevention(self):
        self.app.meshtastic.local_node_id = "!aaaa1111"
        event = MeshtasticTextEvent(
            from_id="!aaaa1111",
            to_id=None,
            packet_id=10,
            reply_id=None,
            channel_index=0,
            text="should not relay",
            sender_label="LocalNode",
        )

        asyncio.run(self.app._dispatch_meshtastic_message(event))
        self.assertEqual(self.app.bot_app.bot.messages, [])

    def test_meshtastic_ping_generates_pong_action(self):
        event = MeshtasticTextEvent(
            from_id="!bbbb2222",
            to_id=None,
            packet_id=11,
            reply_id=None,
            channel_index=3,
            text="PING!!!",
            sender_label="Remote",
        )

        asyncio.run(self.app._dispatch_meshtastic_message(event))

        self.assertEqual(len(self.sent_mesh), 1)
        self.assertEqual(self.sent_mesh[0].text, "Pong")
        self.assertEqual(self.sent_mesh[0].channel_index, 3)
        self.assertEqual(self.sent_mesh[0].reply_id, 11)

    def test_reply_mapping_registered_for_telegram_to_meshtastic(self):
        event = TelegramMessageEvent(
            chat_id=-555,
            message_id=21,
            reply_to_message_id=None,
            text="link this",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        asyncio.run(self.app._dispatch_telegram_message(event))

        self.assertEqual(self.app.reply_links.get_meshtastic_for_telegram(-555, 21), 1001)
        self.assertEqual(self.app.reply_links.get_telegram_for_meshtastic(-555, 1001), 21)

    def test_chunked_message_maps_first_chunk_as_canonical_and_all_chunks_reverse(self):
        event = TelegramMessageEvent(
            chat_id=-555,
            message_id=31,
            reply_to_message_id=None,
            text="x" * 600,
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        asyncio.run(self.app._dispatch_telegram_message(event))

        self.assertGreater(len(self.sent_mesh), 1)
        self.assertEqual(self.app.reply_links.get_meshtastic_for_telegram(-555, 31), 1001)
        self.assertEqual(self.app.reply_links.get_telegram_for_meshtastic(-555, 1001), 31)
        self.assertEqual(self.app.reply_links.get_telegram_for_meshtastic(-555, 1002), 31)

    def test_reply_mapping_registered_for_meshtastic_to_telegram_and_used(self):
        incoming_mesh_event = MeshtasticTextEvent(
            from_id="!bbbb2222",
            to_id=None,
            packet_id=222,
            reply_id=None,
            channel_index=0,
            text="from mesh",
            sender_label="Remote",
        )

        asyncio.run(self.app._dispatch_meshtastic_message(incoming_mesh_event))

        mapped_telegram_message_id = self.app.reply_links.get_telegram_for_meshtastic(-555, 222)
        self.assertIsNotNone(mapped_telegram_message_id)

        telegram_reply_event = TelegramMessageEvent(
            chat_id=-555,
            message_id=22,
            reply_to_message_id=mapped_telegram_message_id,
            text="reply from tg",
            text_source="text",
            is_from_bot=False,
            sender_display_name="Alice",
            has_media=False,
        )

        asyncio.run(self.app._dispatch_telegram_message(telegram_reply_event))

        self.assertEqual(self.sent_mesh[-1].reply_id, 222)

    def test_chunk_sequence_retries_and_completes_after_transient_failure(self):
        attempt_chunk_indexes: list[int] = []
        failure_counter = {"chunk2": 0}

        def _send_with_transient_chunk2_failure(action):
            attempt_chunk_indexes.append(action.sequence_index)
            if action.sequence_index == 2 and failure_counter["chunk2"] == 0:
                failure_counter["chunk2"] += 1
                raise RuntimeError("temporary chunk 2 failure")
            self.sent_mesh.append(action)
            return {"id": 1000 + len(self.sent_mesh)}

        self.app.meshtastic.send_text = _send_with_transient_chunk2_failure

        actions = [
            SendMeshtasticAction(
                text="chunk1",
                sequence_id="seq-1",
                sequence_index=1,
                sequence_total=3,
                retry_max_attempts=3,
                retry_initial_delay_ms=0,
                retry_backoff_factor=2.0,
                abort_on_failure=True,
            ),
            SendMeshtasticAction(
                text="chunk2",
                sequence_id="seq-1",
                sequence_index=2,
                sequence_total=3,
                retry_max_attempts=3,
                retry_initial_delay_ms=0,
                retry_backoff_factor=2.0,
                abort_on_failure=True,
            ),
            SendMeshtasticAction(
                text="chunk3",
                sequence_id="seq-1",
                sequence_index=3,
                sequence_total=3,
                retry_max_attempts=3,
                retry_initial_delay_ms=0,
                retry_backoff_factor=2.0,
                abort_on_failure=True,
            ),
        ]

        asyncio.run(self.app._execute_actions(actions, "bridge"))

        self.assertEqual(attempt_chunk_indexes, [1, 2, 2, 3])
        self.assertEqual([action.sequence_index for action in self.sent_mesh], [1, 2, 3])
        self.assertEqual(self.app.bot_app.bot.messages, [])

    def test_chunk_sequence_aborts_after_terminal_failure(self):
        attempt_chunk_indexes: list[int] = []

        def _send_with_terminal_chunk2_failure(action):
            attempt_chunk_indexes.append(action.sequence_index)
            if action.sequence_index == 2:
                raise RuntimeError("terminal chunk 2 failure")
            self.sent_mesh.append(action)
            return {"id": 2000 + len(self.sent_mesh)}

        self.app.meshtastic.send_text = _send_with_terminal_chunk2_failure

        actions = [
            SendMeshtasticAction(
                text="chunk1",
                sequence_id="seq-2",
                sequence_index=1,
                sequence_total=3,
                retry_max_attempts=2,
                retry_initial_delay_ms=0,
                retry_backoff_factor=2.0,
                abort_on_failure=True,
            ),
            SendMeshtasticAction(
                text="chunk2",
                sequence_id="seq-2",
                sequence_index=2,
                sequence_total=3,
                retry_max_attempts=2,
                retry_initial_delay_ms=0,
                retry_backoff_factor=2.0,
                abort_on_failure=True,
            ),
            SendMeshtasticAction(
                text="chunk3",
                sequence_id="seq-2",
                sequence_index=3,
                sequence_total=3,
                retry_max_attempts=2,
                retry_initial_delay_ms=0,
                retry_backoff_factor=2.0,
                abort_on_failure=True,
            ),
        ]

        with self.assertLogs("meshgram.app", level="ERROR") as log_context:
            asyncio.run(self.app._execute_actions(actions, "bridge"))

        self.assertEqual(attempt_chunk_indexes, [1, 2, 2])
        self.assertEqual([action.sequence_index for action in self.sent_mesh], [1])
        self.assertEqual(self.app.bot_app.bot.messages, [])
        self.assertTrue(
            any("Meshtastic send exhausted retries" in line for line in log_context.output),
            msg=f"Expected retry exhaustion log, got: {log_context.output}",
        )


if __name__ == "__main__":
    unittest.main()

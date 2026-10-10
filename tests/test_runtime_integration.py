import asyncio
import os
import tempfile
import unittest
from unittest import mock

from meshgram.app import MeshgramApp
from meshgram.config import MeshgramSettings, PluginConfig
from meshgram.types import MeshTextEvent, SendMeshAction, TelegramMessageEvent


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


class _FakeMesh:
    payload_limit = 140

    def __init__(self):
        self.is_connected = True
        self.local_node_id = None
        self.sent: list[SendMeshAction] = []
        self.attempts: list[SendMeshAction] = []
        # sequence_index -> exceptions to raise on the next attempts
        self.failures: dict = {}

    def refresh_local_node_id(self):
        pass

    def invalidate_connection(self):
        self.is_connected = False

    async def asend_text(self, action):
        self.attempts.append(action)
        pending = self.failures.get(action.sequence_index)
        if pending:
            raise pending.pop(0)
        self.sent.append(action)
        return f"mc-out-{len(self.sent)}"


def _telegram(text, message_id=1) -> TelegramMessageEvent:
    return TelegramMessageEvent(
        chat_id=-555,
        message_id=message_id,
        text=text,
        text_source="text",
        is_from_bot=False,
        sender_display_name="Alice",
        has_media=False,
    )


def _mesh(text, from_id="bbbb2222cccc", channel_index=0, packet_id="mc-ch-1") -> MeshTextEvent:
    return MeshTextEvent(
        from_id=from_id,
        to_id=None,
        packet_id=packet_id,
        channel_index=channel_index,
        text=text,
        sender_label="Remote",
    )


def _chunk(index, total=3, attempts=3, sequence="seq"):
    return SendMeshAction(
        text=f"chunk{index}",
        sequence_id=sequence,
        sequence_index=index,
        sequence_total=total,
        retry_max_attempts=attempts,
        retry_initial_delay_ms=0,
        retry_backoff_factor=2.0,
        abort_on_failure=True,
    )


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": data_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-555,
            config_path="config.yaml",
            plugins=[
                PluginConfig(name="bridge", enabled=True, settings={}),
                PluginConfig(name="ping_pong", enabled=True, settings={}),
            ],
        )
        settings.meshcore.bridge_channel = 0
        settings.chunking.inter_chunk_delay_ms = 0
        settings.chunking.broadcast_min_inter_chunk_delay_ms = 0
        settings.web.enabled = False

        self.app = MeshgramApp(settings)
        self.app.bot_app = _FakeTelegramApp()
        self.mesh = _FakeMesh()
        self.app.mesh = self.mesh
        await self.app.plugins.start_all()
        self.addAsyncCleanup(self.app.plugins.stop_all)
        # Chunks wait a while between sends; not in tests.
        sleep = asyncio.sleep
        patcher = mock.patch("meshgram.app.asyncio.sleep", lambda _seconds: sleep(0))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_telegram_to_mesh_dispatch(self):
        await self.app._dispatch_telegram_message(_telegram("hello from telegram"))
        self.assertEqual(len(self.mesh.sent), 1)
        self.assertEqual(self.mesh.sent[0].text, "[Alice] hello from telegram")
        self.assertEqual(self.mesh.sent[0].channel_index, 0)

    async def test_long_telegram_message_goes_out_in_chunks(self):
        await self.app._dispatch_telegram_message(_telegram("x" * 600))
        self.assertGreater(len(self.mesh.sent), 1)
        self.assertTrue(self.mesh.sent[0].text.startswith("(1/"))

    async def test_mesh_to_telegram(self):
        await self.app._on_mesh_text(_mesh("from mesh"))
        self.assertEqual(self.app.bot_app.bot.messages, [{"chat_id": -555, "text": "[Remote] from mesh"}])

    async def test_mesh_duplicates_are_dropped(self):
        await self.app._on_mesh_text(_mesh("once"))
        await self.app._on_mesh_text(_mesh("once"))
        self.assertEqual(len(self.app.bot_app.bot.messages), 1)

    async def test_mesh_to_telegram_loop_prevention(self):
        self.mesh.local_node_id = "aaaa1111bbbb"
        await self.app._dispatch_mesh_message(_mesh("should not relay", from_id="aaaa1111bbbb"))
        self.assertEqual(self.app.bot_app.bot.messages, [])

    async def test_mesh_ping_generates_pong(self):
        await self.app._dispatch_mesh_message(_mesh("PING!!!", channel_index=3, packet_id="mc-ch-11"))
        self.assertEqual(len(self.mesh.sent), 1)
        self.assertEqual((self.mesh.sent[0].text, self.mesh.sent[0].channel_index), ("Pong", 3))

    async def test_chunk_sequence_retries_and_completes_after_transient_failure(self):
        self.mesh.failures[2] = [RuntimeError("temporary chunk 2 failure")]
        with self.assertLogs("meshgram.app", level="WARNING"):
            await self.app.execute_actions([_chunk(1), _chunk(2), _chunk(3)], "bridge")
        self.assertEqual([action.sequence_index for action in self.mesh.attempts], [1, 2, 2, 3])
        self.assertEqual([action.sequence_index for action in self.mesh.sent], [1, 2, 3])

    async def test_chunk_sequence_aborts_after_terminal_failure(self):
        self.mesh.failures[2] = [RuntimeError("terminal"), RuntimeError("terminal")]
        with self.assertLogs("meshgram.app", level="ERROR") as log_context:
            await self.app.execute_actions([_chunk(1, attempts=2), _chunk(2, attempts=2), _chunk(3, attempts=2)], "bridge")
        self.assertEqual([action.sequence_index for action in self.mesh.attempts], [1, 2, 2])
        self.assertEqual([action.sequence_index for action in self.mesh.sent], [1])
        self.assertTrue(any("Mesh send exhausted retries" in line for line in log_context.output))

    async def test_connection_errors_drop_the_connection(self):
        self.mesh.failures[None] = [ConnectionError("serial port gone")]
        with self.assertLogs("meshgram.app", level="ERROR"):
            await self.app.execute_actions([SendMeshAction(text="hi")], "bridge")
        self.assertFalse(self.mesh.is_connected)

    async def test_send_while_disconnected_is_dropped(self):
        self.mesh.is_connected = False
        with self.assertLogs("meshgram.app", level="WARNING"):
            self.assertIsNone(await self.app._execute_send_mesh(SendMeshAction(text="hi")))
        self.assertEqual(self.mesh.attempts, [])


if __name__ == "__main__":
    unittest.main()

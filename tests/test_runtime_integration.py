import asyncio
import datetime
import os
import tempfile
import unittest
from unittest import mock

from telegram import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,     MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    Update,
    User,
)

from telegram.ext import ApplicationBuilder

from meshgram.app import MeshgramApp
from meshgram.config import MeshgramSettings, PluginConfig
from meshgram.types import MeshTextEvent, SendMeshAction, TelegramMessageEvent


class _FakeTelegramMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


_BOT_USER = User(id=99, first_name="Meshgram", is_bot=True, username="meshgram_bot")


class _FakeBot:
    id = _BOT_USER.id
    username = _BOT_USER.username

    def __init__(self):
        self.messages = []
        self._next_message_id = 100
        self.member = ChatMemberMember(user=_BOT_USER)
        self.member_error = None

    async def get_chat_member(self, chat_id, user_id):
        if self.member_error is not None:
            raise self.member_error
        return self.member

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


_GROUP = Chat(id=-555, type=Chat.SUPERGROUP, title="Mesh")
_ALICE = User(id=7, first_name="Alice", is_bot=False)
_DATE = datetime.datetime(2026, 10, 31, tzinfo=datetime.timezone.utc)


def _reaction_update(old=(), new=(), message_id=1, user=_ALICE, actor_chat=None) -> Update:
    return Update(
        update_id=1,
        message_reaction=MessageReactionUpdated(
            chat=_GROUP,
            message_id=message_id,
            date=_DATE,
            old_reaction=tuple(old),
            new_reaction=tuple(new),
            user=user,
            actor_chat=actor_chat,
        ),
    )


def _admin() -> ChatMemberAdministrator:
    rights = dict.fromkeys(
        (
            "can_be_edited",
            "is_anonymous",
            "can_manage_chat",
            "can_delete_messages",
            "can_manage_video_chats",
            "can_restrict_members",
            "can_promote_members",
            "can_change_info",
            "can_invite_users",
            "can_post_stories",
            "can_edit_stories",
            "can_delete_stories",
        ),
        False,
    )
    return ChatMemberAdministrator(user=_BOT_USER, **rights)


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
        self.addAsyncCleanup(self.app._save_telegram_texts_now)
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

    def _handled_by(self, update):
        return [handler for handler in self.app._telegram_handlers() if handler.check_update(update)]

    async def test_edited_messages_are_not_handled(self):
        # Telegram sends edited_message updates when reactions change: relaying them sent the message again.
        message = Message(message_id=1, date=_DATE, chat=_GROUP, from_user=_ALICE, text="hello")
        self.assertEqual(len(self._handled_by(Update(update_id=1, message=message))), 1)
        for update in (Update(update_id=2, edited_message=message), Update(update_id=3, edited_channel_post=message)):
            self.assertEqual([handler.callback for handler in self._handled_by(update)], [self.app._handle_telegram_edit])

    async def test_edit_updates_the_quoted_text_without_relaying(self):
        message = Message(message_id=42, date=_DATE, chat=_GROUP, from_user=_ALICE, text="hi al")
        await self.app._handle_telegram_message(Update(update_id=1, message=message), None)
        edited = Message(message_id=42, date=_DATE, chat=_GROUP, from_user=_ALICE, text="hi all")
        await self.app._handle_telegram_edit(Update(update_id=2, edited_message=edited), None)
        self.assertEqual(len(self.mesh.sent), 1)
        await self.app._handle_telegram_reaction(_reaction_update(new=[ReactionTypeEmoji("👍")], message_id=42), None)
        self.assertEqual(self.mesh.sent[-1].text, '[Alice] reacted 👍 to "hi all"')

    async def test_reaction_updates_are_handled(self):
        self.assertEqual(len(self._handled_by(_reaction_update(new=[ReactionTypeEmoji("👍")]))), 1)

    async def test_reaction_to_relayed_message_goes_to_mesh(self):
        await self.app._on_mesh_text(_mesh("from mesh"))
        sent_id = self.app.bot_app.bot._next_message_id
        update = _reaction_update(
            old=[ReactionTypeEmoji("👍")],
            new=[ReactionTypeEmoji("👍"), ReactionTypeEmoji("❤"), ReactionTypeCustomEmoji("123")],
            message_id=sent_id,
        )
        await self.app._handle_telegram_reaction(update, None)
        self.assertEqual([action.text for action in self.mesh.sent], ['[Alice] reacted ❤ to "[Remote] from mesh"'])

    async def test_reaction_to_telegram_message_quotes_it(self):
        message = Message(message_id=42, date=_DATE, chat=_GROUP, from_user=_ALICE, text="hi all")
        await self.app._handle_telegram_message(Update(update_id=1, message=message), None)
        self.mesh.sent.clear()
        bob = User(id=8, first_name="Bob", is_bot=False)
        await self.app._handle_telegram_reaction(
            _reaction_update(new=[ReactionTypeEmoji("😂")], message_id=42, user=bob), None
        )
        self.assertEqual([action.text for action in self.mesh.sent], ['[Bob] reacted 😂 to "hi all"'])

    async def test_removed_custom_or_unknown_reactions_send_nothing(self):
        await self.app._on_mesh_text(_mesh("from mesh"))
        sent_id = self.app.bot_app.bot._next_message_id
        # A reaction taken back, a custom emoji.
        await self.app._handle_telegram_reaction(_reaction_update(old=[ReactionTypeEmoji("👍")], message_id=sent_id), None)
        await self.app._handle_telegram_reaction(_reaction_update(new=[ReactionTypeCustomEmoji("1")], message_id=sent_id), None)
        self.assertEqual(self.mesh.sent, [])

    async def test_raw_telegram_updates_reach_the_radio(self):
        # The Bot API's JSON, through python-telegram-bot's own dispatch: a message, then reactions to it
        # and to a message Meshgram never saw.
        bot_app = ApplicationBuilder().token("1:token").build()
        bot_app.add_handlers(self.app._telegram_handlers())
        bot_app._initialized = True  # initialize() would ask Telegram who the bot is
        chat = {"id": -555, "title": "Group", "type": "supergroup"}
        bob = {"id": 8, "is_bot": False, "first_name": "Bob"}
        updates = [
            {"update_id": 1, "message": {"message_id": 5, "date": 1700000000, "chat": chat,
                                         "from": {"id": 7, "is_bot": False, "first_name": "Alice"}, "text": "hello"}},
            {"update_id": 2, "message_reaction": {"chat": chat, "message_id": 5, "user": bob, "date": 1700000001,
                                                  "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
            {"update_id": 3, "message_reaction": {"chat": chat, "message_id": 77, "user": bob, "date": 1700000002,
                                                  "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "❤"}]}},
        ]
        for data in updates:
            await bot_app.process_update(Update.de_json(data, bot_app.bot))
        self.assertEqual(
            [action.text for action in self.mesh.sent],
            ["[Alice] hello", '[Bob] reacted 👍 to "hello"', "[Bob] reacted ❤"],
        )

    async def test_reaction_to_unknown_message_goes_out_without_quote(self):
        # A photo, a sticker, or a message from before Meshgram kept texts.
        await self.app._handle_telegram_reaction(_reaction_update(new=[ReactionTypeEmoji("👍")], message_id=999), None)
        self.assertEqual([action.text for action in self.mesh.sent], ["[Alice] reacted 👍"])

    async def test_reaction_to_message_from_before_a_restart_goes_to_mesh(self):
        message = Message(message_id=42, date=_DATE, chat=_GROUP, from_user=_ALICE, text="before restart")
        await self.app._handle_telegram_message(Update(update_id=1, message=message), None)
        await self.app._save_telegram_texts_now()

        restarted = MeshgramApp(self.app.settings)
        restarted.bot_app = _FakeTelegramApp()
        restarted.mesh = self.mesh
        self.addAsyncCleanup(restarted._save_telegram_texts_now)
        await restarted.plugins.start_all()
        self.addAsyncCleanup(restarted.plugins.stop_all)
        self.mesh.sent.clear()
        await restarted._handle_telegram_reaction(_reaction_update(new=[ReactionTypeEmoji("👍")], message_id=42), None)
        self.assertEqual([action.text for action in self.mesh.sent], ['[Alice] reacted 👍 to "before restart"'])

    async def test_bot_that_is_not_an_admin_says_so(self):
        self.app._set_telegram_connected("meshgram_bot")
        # Telegram sends reactions to admins only; otherwise they silently never arrive.
        with self.assertLogs("meshgram.app", level="WARNING") as logs:
            await self.app._check_telegram_admin(self.app.bot_app)
        self.assertIn("isn't an admin", "\n".join(logs.output))
        self.assertFalse(self.app._telegram_is_admin)
        self.assertIn("not a group admin", self.app.status.get("telegram")["detail"])

        promoted = ChatMemberUpdated(
            chat=_GROUP,
            from_user=_ALICE,
            date=_DATE,
            old_chat_member=ChatMemberMember(user=_BOT_USER),
            new_chat_member=_admin(),
        )
        update = Update(update_id=5, my_chat_member=promoted)
        self.assertEqual(len(self._handled_by(update)), 1)
        context = mock.Mock(bot=self.app.bot_app.bot)
        with self.assertLogs("meshgram.app", level="INFO"):
            await self.app._handle_my_chat_member(update, context)
        self.assertTrue(self.app._telegram_is_admin)
        self.assertEqual(self.app.status.get("telegram")["detail"], "@meshgram_bot")

    async def test_admin_bot_and_failed_check(self):
        self.app._set_telegram_connected("meshgram_bot")
        self.app.bot_app.bot.member = _admin()
        with self.assertLogs("meshgram.app", level="INFO"):
            await self.app._check_telegram_admin(self.app.bot_app)
        self.assertTrue(self.app._telegram_is_admin)
        self.assertEqual(self.app.status.get("telegram")["detail"], "@meshgram_bot")

        self.app._telegram_is_admin = None
        self.app.bot_app.bot.member_error = RuntimeError("chat not found")
        with self.assertLogs("meshgram.app", level="WARNING"):
            await self.app._check_telegram_admin(self.app.bot_app)
        self.assertIsNone(self.app._telegram_is_admin)

    async def test_restart_stops_the_bot_for_main_to_start_again(self):
        self.app.bot_app.stop_running = mock.Mock()
        with mock.patch("meshgram.app.RESTART_DELAY_SECONDS", 0), self.assertLogs("meshgram.app", level="INFO"):
            self.app.request_restart()
        self.assertTrue(self.app.restart_requested)
        self.app.bot_app.stop_running.assert_not_called()  # Not before the web app has answered.
        for _ in range(3):
            await asyncio.sleep(0)
        self.app.bot_app.stop_running.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from meshgram.config import MeshgramSettings
from meshgram.plugins.bridge import BridgePlugin, normalize_user, truncate_utf8
from meshgram.settings_schema import validate
from meshgram.text_utils import utf8_len
from meshgram.types import MeshTextEvent, PluginContext, TelegramMessageEvent, TelegramReactionEvent


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


def _reaction(emojis=("👍",), message_text="[Alpha] hello mesh", **overrides) -> TelegramReactionEvent:
    fields = dict(
        chat_id=-999,
        message_id=10,
        emojis=emojis,
        is_from_bot=False,
        sender_display_name="Alice Smith",
        message_text=message_text,
    )
    fields.update(overrides)
    return TelegramReactionEvent(**fields)


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

    def _react(self, event, payload_limit=80, plugin=None):
        return asyncio.run((plugin or self.plugin).on_telegram_reaction(event, self._context(payload_limit=payload_limit)))

    def test_reaction_goes_out_as_one_message_quoting_the_message(self):
        actions = self._react(_reaction(emojis=("👍", "🔥")))
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text, '[Alice] reacted 👍🔥 to "[Alpha] hello mesh"')
        self.assertEqual(actions[0].channel_index, 2)
        self.assertIsNone(actions[0].sequence_id)

    def test_reaction_to_unknown_message_goes_out_without_quote(self):
        for text in (None, "  "):
            actions = self._react(_reaction(message_text=text))
            self.assertEqual([action.text for action in actions], ["[Alice] reacted 👍"])

    def test_reaction_ignores_bots_other_chats_and_no_emoji(self):
        self.assertEqual(self._react(_reaction(is_from_bot=True)), [])
        self.assertEqual(self._react(_reaction(chat_id=-1)), [])
        self.assertEqual(self._react(_reaction(emojis=())), [])

    def test_reactions_can_be_turned_off(self):
        self.assertEqual(self._react(_reaction(), plugin=BridgePlugin({"relay_reactions": False})), [])

    def test_reaction_shortens_long_quote_to_one_packet(self):
        actions = self._react(_reaction(message_text="é" * 200), payload_limit=80)
        self.assertEqual(len(actions), 1)
        text = actions[0].text
        self.assertLessEqual(utf8_len(text), 64)  # payload limit 80 minus the 16-byte margin
        self.assertTrue(text.startswith('[Alice] reacted 👍 to "éé'))
        self.assertTrue(text.endswith('é…"'))

    def test_settings_schema(self):
        validate({"channel": 3}, BridgePlugin.settings_schema)
        with self.assertRaises(ValueError):
            validate({"channel": "three"}, BridgePlugin.settings_schema)


SOS = {
    "channel": 5,
    "telegram_group_id": -555,
    "sender_prefix_template": "<{display_name}> {message}",
    "long_messages": "truncate",
    "senders": {"1001": "Thomas", "@AnnaTG": "Anna", 1003: "Thomas"},
}


class SosBridgeTests(BridgePluginTests):
    """A second bridge for an SOS channel: its own group, and messages sent in the sender's name."""

    def setUp(self):
        super().setUp()
        self.sos = BridgePlugin(dict(SOS))
        self.sos.instance_id = "sos"

    def _sos(self, text="ack", payload_limit=120, **fields):
        fields.setdefault("chat_id", -555)
        return self._send_telegram(_telegram(text, **fields), payload_limit=payload_limit, plugin=self.sos)

    def test_each_bridge_relays_only_its_own_group_and_channel(self):
        self.assertEqual(self._sos(chat_id=-999), [])
        self.assertEqual(self._send_telegram(_telegram(chat_id=-555)), [])
        actions = self._sos("fire")
        self.assertEqual([(action.channel_index, action.text) for action in actions], [(5, "<Alice> fire")])

        self.assertEqual(asyncio.run(self.sos.on_mesh_message(_mesh(channel_index=2), self._context())), [])
        actions = asyncio.run(self.sos.on_mesh_message(_mesh("SOS! Need help. #a3f1", channel_index=5), self._context()))
        self.assertEqual([(action.chat_id, action.text) for action in actions], [(-555, "[Alpha] SOS! Need help. #a3f1")])

    def test_mapped_users_send_in_their_boards_name(self):
        self.assertEqual(self._sos(sender_id=1001, sender_display_name="Tom L")[0].text, "<Thomas> ack")
        # Several Telegram users can share a board.
        self.assertEqual(self._sos(sender_id=1003, sender_display_name="Other Phone")[0].text, "<Thomas> ack")
        # By username, whatever its case; the ID wins when both match.
        self.assertEqual(self._sos(sender_id=7, sender_username="annatg")[0].text, "<Anna> ack")
        self.assertEqual(self._sos(sender_id=1001, sender_username="AnnaTG")[0].text, "<Thomas> ack")
        # Nobody's: their Telegram first name.
        self.assertEqual(self._sos(sender_id=9, sender_display_name="Bob Builder")[0].text, "<Bob> ack")

    def test_mapping_also_names_senders_with_the_usual_prefix(self):
        plugin = BridgePlugin({"senders": {"1001": "SOS Bench B"}})
        actions = self._send_telegram(_telegram(sender_id=1001), plugin=plugin)
        self.assertEqual(actions[0].text, "[SOS Bench B] hello")

    def test_unmapped_names_are_made_safe_for_the_relay_mark(self):
        self.assertEqual(self._sos(sender_display_name="<A>:B")[0].text, "<AB> ack")
        self.assertEqual(self._sos(sender_display_name="<>")[0].text, "<Telegram> ack")
        self.assertEqual(self._sos(sender_display_name="x" * 40)[0].text, f"<{'x' * 31}> ack")
        # The usual prefix leaves names as they are.
        self.assertEqual(self._send_telegram(_telegram(sender_display_name="<A>"))[0].text, "[<A>] hello")

    def test_long_messages_are_cut_to_one_radio_message(self):
        actions = self._sos("help " * 60, payload_limit=60, sender_id=1001)
        self.assertEqual(len(actions), 1)
        self.assertIsNone(actions[0].sequence_id)
        self.assertTrue(actions[0].text.startswith("<Thomas> help help"))
        self.assertTrue(actions[0].text.endswith("…"))
        self.assertLessEqual(utf8_len(actions[0].text), 60 - 16)

    def test_reactions_use_the_bridges_own_group_prefix_and_names(self):
        send = lambda plugin, **fields: asyncio.run(plugin.on_telegram_reaction(_reaction(**fields), self._context(payload_limit=120)))
        # The main group's reactions aren't the SOS bridge's, and the other way round.
        self.assertEqual(send(self.sos), [])
        self.assertEqual(send(self.plugin, chat_id=-555), [])
        actions = send(self.sos, chat_id=-555, sender_id=1001, message_text=None)
        self.assertEqual([(action.channel_index, action.text) for action in actions], [(5, "<Thomas> reacted 👍")])
        # Off, as an SOS bridge should have it: nothing goes out.
        quiet = BridgePlugin({**SOS, "relay_reactions": False})
        self.assertEqual(send(quiet, chat_id=-555, sender_id=1001), [])

    def test_truncate_utf8(self):
        self.assertEqual(truncate_utf8("short", 10), "short")
        self.assertEqual(truncate_utf8("héllo wörld", 8), "héll…")
        self.assertLessEqual(utf8_len(truncate_utf8("😀" * 10, 9)), 9)

    def test_normalize_user(self):
        self.assertEqual(normalize_user("1001"), 1001)
        self.assertEqual(normalize_user(1001), 1001)
        self.assertEqual(normalize_user(" @AnnaTG "), "annatg")
        self.assertIsNone(normalize_user("@"))
        self.assertIsNone(normalize_user(True))

    def test_settings_schema_for_an_sos_bridge(self):
        validate({**SOS, "senders": {"1001": "Thomas"}, "telegram_bot_token": "123:abc"}, BridgePlugin.settings_schema)
        for bad in (
            {"senders": {"1001": "A>B"}},
            {"senders": {"1001": "x" * 32}},
            {"long_messages": "drop"},
            {"telegram_group_id": "-555"},
            {"telegram_bot_token": "nope"},
        ):
            with self.assertRaises(ValueError, msg=bad):
                validate(bad, BridgePlugin.settings_schema)


class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.dir = tempdir.name
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": tempdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = MeshgramSettings(telegram_bot_token="1:main", telegram_group_id=-999, config_path="config.yaml", plugins=[])
        self.executed = []

    async def _execute(self, actions, plugin_name):
        self.executed.append((plugin_name, [action.text for action in actions]))

    def _context(self):
        return PluginContext(
            settings=self.settings, telegram_group_id=-999, mesh_payload_limit=120, local_node_id=None,
            execute_actions=self._execute,
        )

    async def _started(self, settings, instance_id="sos"):
        plugin = BridgePlugin(dict(settings))
        plugin.instance_id = instance_id
        await plugin.on_startup(self._context())
        self.addAsyncCleanup(plugin.on_shutdown)
        return plugin

    async def test_an_added_bridge_needs_its_own_channel(self):
        plugin = BridgePlugin({})
        plugin.instance_id = "sos"
        with self.assertRaisesRegex(ValueError, "radio channel"):
            await plugin.on_startup(self._context())
        # The bridge itself still falls back to meshcore.bridge_channel.
        await self._started({}, instance_id=None)

    async def test_people_seen_in_the_group_are_remembered(self):
        plugin = await self._started(SOS)
        self.assertEqual(plugin.instance_name, "bridge@sos")
        await plugin.on_telegram_message(_telegram("ack", chat_id=-555, sender_id=1001, sender_username="tom", sender_display_name="Tom  L"), self._context())
        await plugin.on_telegram_message(_telegram("x", chat_id=-555, sender_id=9, sender_display_name="Bob"), self._context())
        await plugin.on_telegram_message(_telegram("x", chat_id=-999, sender_id=8, sender_display_name="Elsewhere"), self._context())
        await plugin.on_mesh_message(_mesh("SOS!", channel_index=5), self._context())

        people = plugin.people()
        self.assertEqual({person["id"]: (person["name"], person["username"], person["radio_name"]) for person in people["people"]},
                         {1001: ("Tom L", "tom", "Thomas"), 9: ("Bob", None, None)})
        self.assertEqual(people["nodes"], ["Alpha", "Thomas", "Anna"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "bridge_sos_people.json")))

        # They're still known after a restart, and each bridge keeps its own.
        again = await self._started(SOS)
        self.assertEqual({person["id"] for person in again.people()["people"]}, {1001, 9})
        other = await self._started({"channel": 1}, instance_id=None)
        self.assertEqual(other.people()["people"], [])

    async def test_new_senders_apply_without_a_restart(self):
        plugin = await self._started(SOS)
        self.assertTrue(await plugin.apply_settings({**SOS, "senders": {"9": "Bob Board"}}))
        actions = await plugin.on_telegram_message(_telegram("ack", chat_id=-555, sender_id=9), self._context())
        self.assertEqual(actions[0].text, "<Bob Board> ack")
        # Another bot, or no channel any more, needs a restart.
        self.assertFalse(await plugin.apply_settings({**SOS, "telegram_bot_token": "2:other"}))
        self.assertFalse(await plugin.apply_settings({key: value for key, value in SOS.items() if key != "channel"}))
        self.assertEqual(plugin.settings["senders"], {"9": "Bob Board"})

    async def test_the_main_bots_token_is_not_a_bot_of_its_own(self):
        plugin = await self._started({**SOS, "telegram_bot_token": "1:main"})
        self.assertIsNone(plugin._own_bot_task)
        actions = await plugin.on_telegram_message(_telegram("ack", chat_id=-555), self._context())
        self.assertEqual(len(actions), 1)

    async def test_a_bridge_with_its_own_bot_reads_and_writes_through_it(self):
        sent = []

        class FakeBot:
            username = "sos_bot"

            async def send_message(self, chat_id, text):
                sent.append((chat_id, text))

            async def get_me(self):
                return self

        class FakeUpdater:
            running = False

            async def start_polling(self, **kwargs):
                self.running = True

            async def stop(self):
                self.running = False

        class FakeApplication:
            def __init__(self):
                self.bot, self.updater, self.running, self.handlers, self.stopped = FakeBot(), FakeUpdater(), False, [], False

            def add_handler(self, handler):
                self.handlers.append(handler)

            async def initialize(self):
                pass

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

            async def shutdown(self):
                self.stopped = True

        application = FakeApplication()
        tokens = []

        class FakeBuilder:
            def token(self, token):
                tokens.append(token)
                return self

            def build(self):
                return application

        with mock.patch("telegram.ext.ApplicationBuilder", FakeBuilder):
            plugin = await self._started({**SOS, "telegram_bot_token": "2:sos"})
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual(tokens, ["2:sos"])
        self.assertIs(plugin._own_bot, application)

        # The app's bot isn't this bridge's: what it hears is ignored, even from the same group.
        self.assertEqual(await plugin.on_telegram_message(_telegram("ack", chat_id=-555, sender_id=1001), self._context()), [])

        message = SimpleNamespace(
            chat=SimpleNamespace(id=-555), message_id=4, text="ack", caption=None,
            from_user=SimpleNamespace(id=1001, full_name="Tom L", username="tom", is_bot=False),
        )
        await plugin._on_own_bot_update(SimpleNamespace(effective_message=message), None)
        self.assertEqual(self.executed, [("bridge@sos", ["<Thomas> ack"])])

        self.assertEqual(await plugin.on_telegram_reaction(_reaction(chat_id=-555), self._context()), [])

        # Radio -> Telegram goes out through its own bot, not as an action for the app's.
        self.assertEqual(await plugin.on_mesh_message(_mesh("SOS!", channel_index=5), self._context()), [])
        self.assertEqual(sent, [(-555, "[Alpha] SOS!")])

        await plugin.on_shutdown()
        self.assertTrue(application.stopped)
        self.assertFalse(application.updater.running)


if __name__ == "__main__":
    unittest.main()


class SosModeTests(BridgeLifecycleTests):
    """mode: sos hands the group to a SosDesk (tests/test_sos_desk.py has the desk itself)."""

    def setUp(self):
        super().setUp()
        from tests.test_sos_desk import FakeBot

        self.bot = FakeBot()

    def _context(self):
        context = super()._context()
        context.telegram_bot = self.bot
        return context

    def _button(self, data, **overrides):
        from meshgram.types import TelegramButtonEvent

        fields = dict(chat_id=-555, message_id=1, data=data, query_id="q", sender_display_name="Thomas Example", sender_id=1001)
        fields.update(overrides)
        return TelegramButtonEvent(**fields)

    async def _sos_bridge(self, **settings):
        return await self._started({"channel": 3, "telegram_group_id": -555, "mode": "sos", "senders": {"1001": "LMB-T HQ"}, **settings})

    async def test_radio_alarm_becomes_a_message_with_buttons_through_the_main_bot(self):
        plugin = await self._sos_bridge()
        actions = await plugin.on_mesh_message(_mesh("SOS! Need help. #83d3", channel_index=3), self._context())
        self.assertEqual(actions, [])
        (message,) = self.bot.find("SOS: Alpha")
        self.assertEqual(len(self.bot.messages[message]["buttons"]), 2)
        # Another channel is none of its business.
        await plugin.on_mesh_message(_mesh("hello", channel_index=2), self._context())
        self.assertEqual(len(self.bot.messages), 1)

    async def test_a_press_goes_out_under_the_mapped_name_and_lists_the_person(self):
        plugin = await self._sos_bridge()
        await plugin.on_mesh_message(_mesh("SOS! Need help. #83d3", channel_index=3), self._context())
        (message,) = self.bot.find("SOS: Alpha")
        await plugin.on_telegram_button(self._button(self.bot.button(message, "ACK")), self._context())
        self.assertEqual(self.executed, [("bridge@sos", ["<LMB-T HQ> ACK #83d3 I am aware"])])
        self.assertEqual([person["id"] for person in plugin.people()["people"]], [1001])

    async def test_an_unmapped_person_sends_under_their_first_name(self):
        plugin = await self._sos_bridge()
        await plugin.on_mesh_message(_mesh("SOS! Need help. #83d3", channel_index=3), self._context())
        (message,) = self.bot.find("SOS: Alpha")
        press = self._button(self.bot.button(message, "ACK"), sender_id=7, sender_display_name="Anna: <Rossi>")
        await plugin.on_telegram_button(press, self._context())
        self.assertEqual(self.executed, [("bridge@sos", ["<Anna> ACK #83d3 I am aware"])])

    async def test_presses_elsewhere_and_other_buttons_are_ignored(self):
        plugin = await self._sos_bridge()
        await plugin.on_telegram_button(self._button("ms:sos", chat_id=-999), self._context())
        await plugin.on_telegram_button(self._button("other:thing"), self._context())
        self.assertEqual((self.bot.messages, self.bot.answers, self.executed), ({}, [], []))

    async def test_typed_text_and_reactions_are_not_sent_to_the_radio(self):
        plugin = await self._sos_bridge()
        typed = _telegram("ack", chat_id=-555, sender_id=1001)
        self.assertEqual(await plugin.on_telegram_message(typed, self._context()), [])
        self.assertEqual(len(self.bot.find("Use the buttons")), 1)
        self.assertEqual(await plugin.on_telegram_reaction(_reaction(chat_id=-555), self._context()), [])
        self.assertEqual(self.executed, [])
        self.assertEqual(len(plugin.people()["people"]), 1)

    async def test_chat_bridges_ignore_presses(self):
        plugin = await self._started({"channel": 3, "telegram_group_id": -555})
        await plugin.on_telegram_button(self._button("ms:sos"), self._context())
        self.assertEqual((self.bot.messages, self.bot.answers), ({}, []))

    async def test_changing_the_mode_restarts_the_bridge(self):
        plugin = await self._sos_bridge()
        self.assertTrue(await plugin.apply_settings({**plugin.settings, "senders": {"7": "Anna"}}))
        self.assertFalse(await plugin.apply_settings({**plugin.settings, "mode": "chat"}))
        self.assertEqual(plugin.settings["mode"], "sos")

    def test_mode_is_in_the_schema(self):
        validate({"mode": "sos"}, BridgePlugin.settings_schema)
        with self.assertRaises(ValueError):
            validate({"mode": "panic"}, BridgePlugin.settings_schema)


class ButtonEventTests(unittest.TestCase):
    def test_event_from_a_callback_query(self):
        from meshgram.telegram_events import button_event_from_query

        user = SimpleNamespace(id=1001, full_name="Thomas Example", username="tex")
        message = SimpleNamespace(message_id=42, chat=SimpleNamespace(id=-555))
        event = button_event_from_query(SimpleNamespace(id="q1", data="ms:sos", message=message, from_user=user))
        self.assertEqual(
            (event.chat_id, event.message_id, event.data, event.query_id, event.sender_id, event.sender_username),
            (-555, 42, "ms:sos", "q1", 1001, "tex"),
        )
        self.assertIsNone(button_event_from_query(SimpleNamespace(id="q", data=None, message=message, from_user=user)))
        self.assertIsNone(button_event_from_query(SimpleNamespace(id="q", data="x", message=None, from_user=user)))
        self.assertIsNone(button_event_from_query(None))

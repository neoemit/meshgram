import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from meshgram import sos_desk
from meshgram.sos_desk import SosDesk
from meshgram.types import TelegramButtonEvent, TelegramMessageEvent


class FakeBot:
    """Records what the desk asks of Telegram."""

    def __init__(self):
        self.messages = {}  # message_id -> {"text", "buttons", "reply_to"}
        self.deleted = []
        self.answers = []  # (query_id, text, alert)
        self.pinned = []
        self.next_id = 100
        self.fail_pin = False
        self.edit_error = None  # raised by every edit while set
        self.send_error = None  # raised by every send while set
        self.edits = 0

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.next_id += 1
        self.messages[self.next_id] = {
            "text": text,
            "buttons": _buttons(reply_markup),
            "reply_to": kwargs.get("reply_to_message_id"),
        }
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self.edits += 1
        if self.edit_error is not None:
            raise self.edit_error
        if message_id not in self.messages:
            raise RuntimeError("Message to edit not found")
        if (self.messages[message_id]["text"], self.messages[message_id]["buttons"]) == (text, _buttons(reply_markup)):
            raise RuntimeError("Message is not modified")
        self.messages[message_id].update(text=text, buttons=_buttons(reply_markup))

    async def delete_message(self, chat_id, message_id):
        self.messages.pop(message_id, None)
        self.deleted.append(message_id)

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answers.append((callback_query_id, text, show_alert))

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        if self.fail_pin:
            raise RuntimeError("not enough rights")
        self.pinned.append(message_id)

    def find(self, fragment):
        return [mid for mid, message in self.messages.items() if fragment in message["text"]]

    def button(self, message_id, label_fragment):
        return next(data for label, data in self.messages[message_id]["buttons"] if label_fragment in label)


def _buttons(markup):
    if markup is None:
        return []
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


USERS = {1: "Thomas Example", 2: "Anna Rossi", 3: "Bob Stone", 4: "Anna Bianchi", 5: "Anna"}


class SosDeskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sos.json"
        self.bot = FakeBot()
        self.sent = []  # radio
        self.online = True
        self.clock = 1000.0
        self.presses = 0
        self.desk = self._desk()

    def _desk(self):
        async def send_mesh(text):
            self.sent.append(text)

        desk = SosDesk(
            label="bridge@sos",
            state_path=self.path,
            bot=lambda: self.bot,
            chat_id=lambda: -5,
            send_mesh=send_mesh,
            radio_online=lambda: self.online,
            now=lambda: self.clock,
        )
        desk._load()
        return desk

    async def press(self, user, data, message_id=0):
        self.presses += 1
        await self.desk.on_button(
            TelegramButtonEvent(
                chat_id=-5, message_id=message_id, data=data, query_id=f"q{self.presses}",
                sender_display_name=USERS[user], sender_id=user,
            )
        )
        return self.bot.answers[-1]

    async def press_on(self, user, fragment, label):
        (message_id,) = self.bot.find(fragment)
        return await self.press(user, self.bot.button(message_id, label), message_id)

    async def send_sos(self, user=1):
        await self.press(user, sos_desk.SOS)
        (question,) = self.bot.find("send an SOS?")
        return await self.press(user, self.bot.button(question, "Yes"), question)

    # --- The panel ------------------------------------------------------------------------------

    async def test_panel_is_posted_once_and_pinned(self):
        await self.desk.tick()
        (panel,) = self.bot.find("MeshSOS")
        self.assertEqual(self.bot.pinned, [panel])
        self.assertEqual(self.bot.messages[panel]["buttons"], [(sos_desk.SOS_LABEL, sos_desk.SOS)])
        await self.desk.tick()
        self.desk = self._desk()  # a restart
        await self.desk.tick()
        self.assertEqual(self.bot.find("MeshSOS"), [panel])

    async def test_panel_is_posted_again_when_it_was_deleted(self):
        await self.desk.tick()
        (panel,) = self.bot.find("MeshSOS")
        del self.bot.messages[panel]
        self.desk = self._desk()
        await self.desk.tick()
        self.assertEqual(len(self.bot.find("MeshSOS")), 1)
        self.assertNotEqual(self.bot.find("MeshSOS"), [panel])

    async def test_panel_is_not_posted_again_when_telegram_does_not_answer(self):
        # A restart while Telegram is unreachable or busy: the pinned SOS button stays as it is,
        # since a new pin in the group reads as an emergency.
        for error in (
            "Timed out",
            "Bad Gateway",
            "httpx.ConnectError: All connection attempts failed",
            "Flood control exceeded. Retry in 7 seconds",
            "Forbidden: bot was kicked from the supergroup chat",
        ):
            with self.subTest(error=error):
                self.setUp()
                await self.desk.tick()
                (panel,) = self.bot.find("MeshSOS")
                self.bot.edit_error = RuntimeError(error)
                self.desk = self._desk()  # a restart
                with self.assertLogs("meshgram.sos_desk", level="WARNING"):
                    await self.desk.tick()
                self.assertEqual(self.bot.find("MeshSOS"), [panel])
                self.assertEqual(self.bot.pinned, [panel])

                # Asked again later, not every round.
                edits = self.bot.edits
                self.clock += 1
                await self.desk.tick()
                self.assertEqual(self.bot.edits, edits)
                self.clock += sos_desk.PANEL_RETRY_SECONDS
                with self.assertLogs("meshgram.sos_desk", level="WARNING"):
                    await self.desk.tick()
                self.assertEqual(self.bot.edits, edits + 1)

                # Telegram answers again: the panel is there, so nothing new is posted or pinned.
                self.bot.edit_error = None
                self.clock += 2 * sos_desk.PANEL_RETRY_SECONDS
                await self.desk.tick()
                self.assertEqual(self.bot.edits, edits + 2)
                self.clock += sos_desk.PANEL_RETRY_MAX_SECONDS
                await self.desk.tick()
                self.assertEqual(self.bot.edits, edits + 2)
                self.assertEqual(self.bot.find("MeshSOS"), [panel])
                self.assertEqual(self.bot.pinned, [panel])

    async def test_panel_waits_longer_after_each_unanswered_check(self):
        await self.desk.tick()
        self.bot.edit_error = RuntimeError("Timed out")
        self.desk = self._desk()
        delays = []
        with self.assertLogs("meshgram.sos_desk", level="WARNING"):
            for _ in range(10):
                await self.desk.tick()
                delays.append(self.desk._panel_retry_at - self.clock)
                self.clock = self.desk._panel_retry_at
        self.assertEqual(delays[:3], [sos_desk.PANEL_RETRY_SECONDS * factor for factor in (1, 2, 4)])
        self.assertEqual(delays[-1], sos_desk.PANEL_RETRY_MAX_SECONDS)
        self.assertEqual(len(self.bot.pinned), 1)

    async def test_panel_another_bot_posted_is_replaced(self):
        await self.desk.tick()
        (panel,) = self.bot.find("MeshSOS")
        self.bot.edit_error = RuntimeError("Message can't be edited")
        self.desk = self._desk()
        await self.desk.tick()
        self.assertEqual(len(self.bot.find("MeshSOS")), 2)
        self.assertEqual(len(self.bot.pinned), 2)
        self.assertNotEqual(self.bot.pinned[-1], panel)

    async def test_panel_is_posted_later_when_telegram_does_not_take_it(self):
        self.bot.send_error = RuntimeError("Timed out")
        with self.assertLogs("meshgram.sos_desk", level="WARNING"):
            await self.desk.tick()
        self.bot.send_error = None
        self.clock += 1
        await self.desk.tick()
        self.assertEqual(self.bot.find("MeshSOS"), [])
        self.clock += sos_desk.PANEL_RETRY_SECONDS
        await self.desk.tick()
        (panel,) = self.bot.find("MeshSOS")
        self.assertEqual(self.bot.pinned, [panel])

    async def test_panel_survives_a_bot_that_may_not_pin(self):
        self.bot.fail_pin = True
        with self.assertLogs("meshgram.sos_desk", level="WARNING"):
            await self.desk.tick()
        self.assertEqual(len(self.bot.find("MeshSOS")), 1)

    # --- An emergency from the radio ------------------------------------------------------------

    async def test_radio_alarm_shows_with_buttons_and_repeats_are_not_shown_again(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. bat 97% #83d3")
        (message,) = self.bot.find("SOS: LMB-S HQ")
        self.assertIn("SOS! Need help. bat 97%", self.bot.messages[message]["text"])
        self.assertNotIn("83d3", self.bot.messages[message]["text"])
        self.assertIn("Nobody has acknowledged yet", self.bot.messages[message]["text"])
        self.assertEqual(len(self.bot.messages[message]["buttons"]), 2)
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. bat 96% #83d3")
        self.assertEqual(len(self.bot.messages), 1)

    async def test_ack_button_sends_an_ack_under_the_pressers_first_name(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        answer = await self.press_on(1, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(self.sent, ["<Thomas> ACK #83d3 I am aware"])
        self.assertEqual(answer[1:], ("ACK sent.", False))
        (message,) = self.bot.find("SOS: LMB-S HQ")
        self.assertIn("✅ Aware: Thomas", self.bot.messages[message]["text"])
        (notice,) = self.bot.find("is aware")
        self.assertEqual(self.bot.messages[notice]["reply_to"], message)
        # A second press sends nothing more.
        answer = await self.press_on(1, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("already listed", answer[1])

    async def test_ack_from_the_radio_is_listed(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        await self.desk.on_mesh("LMB-T EDC", "ack")
        await self.desk.on_mesh("OtherGw", "<Carl> ACK #83d3 I am aware")
        await self.desk.on_mesh("LMB-T EDC", "ok")  # again
        await self.desk.on_mesh("LMB-S HQ", "ack")  # the caller
        await self.desk.on_mesh("Dora", "ACK #9999 I am aware")  # about another emergency
        (message,) = self.bot.find("SOS: LMB-S HQ")
        self.assertIn("✅ Aware: LMB-T EDC, Carl", self.bot.messages[message]["text"])
        self.assertNotIn("Dora", self.bot.messages[message]["text"])
        self.assertEqual(self.sent, [])

    async def test_resolved_asks_first_and_only_the_one_who_pressed_can_answer(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        await self.press_on(1, "SOS: LMB-S HQ", "Resolved")  # nobody acknowledged yet: fine
        self.assertEqual(self.sent, [])
        (question,) = self.bot.find("<b>Thomas</b>, is the emergency over?")
        answer = await self.press(2, self.bot.button(question, "Yes"), question)
        self.assertEqual(answer[1:], ("This question is for Thomas.", True))
        self.assertEqual(self.sent, [])
        await self.press(1, self.bot.button(question, "Yes"), question)
        self.assertEqual(self.sent, ["<Thomas> RESOLVED #83d3 all OK"])
        self.assertNotIn(question, self.bot.messages)
        (message,) = self.bot.find("Resolved: LMB-S HQ")
        self.assertEqual(self.bot.messages[message]["buttons"], [])
        self.assertIn("Resolved by Thomas", self.bot.messages[message]["text"])
        self.assertIsNone(self.desk.emergency)
        # A late repeat of the closed emergency stays closed.
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        self.assertIsNone(self.desk.emergency)
        # A new one opens.
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d4")
        self.assertIsNotNone(self.desk.emergency)

    async def test_cancel_and_timeout_send_nothing(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        await self.press_on(1, "SOS: LMB-S HQ", "Resolved")
        (question,) = self.bot.find("emergency over?")
        await self.press(1, self.bot.button(question, "Cancel"), question)
        self.assertNotIn(question, self.bot.messages)
        await self.press_on(1, "SOS: LMB-S HQ", "Resolved")
        (question,) = self.bot.find("emergency over?")
        data = self.bot.button(question, "Yes")
        self.clock += sos_desk.CONFIRM_SECONDS + 1
        await self.desk.tick()
        self.assertNotIn(question, self.bot.messages)
        answer = await self.press(1, data, question)
        self.assertIn("expired", answer[1])
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(self.desk.emergency)

    async def test_resolved_from_the_radio_closes_it_and_old_buttons_say_so(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        (message,) = self.bot.find("SOS: LMB-S HQ")
        ack = self.bot.button(message, "ACK")
        await self.desk.on_mesh("LMB-T EDC", "RESOLVED #9999 all OK")  # about another emergency
        self.assertIsNotNone(self.desk.emergency)
        await self.desk.on_mesh("LMB-T EDC", "safe")  # anyone, typed by hand
        self.assertIsNone(self.desk.emergency)
        self.assertTrue(self.bot.find("Resolved by <b>LMB-T EDC</b>"))
        answer = await self.press(1, ack, message)
        self.assertEqual(answer[1:], ("This emergency is closed.", True))
        # ... also when another emergency is open by then.
        await self.desk.on_mesh("Anna", "fire at the gate")
        await self.press(1, ack, message)
        self.assertEqual(self.sent, [])

    async def test_typed_alarm_has_no_id_and_any_ack_counts(self):
        await self.desk.on_mesh("Bob", "fire at the gate")
        await self.desk.on_mesh("Dora", "ACK #1234 I am aware")
        await self.press_on(1, "SOS: Bob", "ACK")
        self.assertEqual(self.sent, ["<Thomas> ACK I am aware"])
        self.assertEqual(self.desk.emergency.aware, ["Dora", "Thomas"])

    async def test_more_text_from_the_caller_moves_the_emergency_to_a_new_message(self):
        await self.desk.on_mesh("Anna", "fire at the gate")
        (first,) = self.bot.find("fire at the gate")
        await self.desk.on_mesh("Bob", "ok")
        await self.desk.on_mesh("Anna", "it is spreading")
        self.assertEqual(self.bot.messages[first]["buttons"], [])
        (second,) = self.bot.find("it is spreading")
        self.assertEqual(len(self.bot.messages[second]["buttons"]), 2)
        self.assertIn("✅ Aware: Bob", self.bot.messages[second]["text"])
        self.assertEqual(self.desk.emergency.callers, ["Anna"])

    async def test_a_second_caller_joins_the_one_emergency(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #0001")
        await self.desk.on_mesh("LMB-T EDC", "SOS! Need help. #0002")  # pressed at the same moment
        self.assertEqual(self.desk.emergency.callers, ["LMB-S HQ", "LMB-T EDC"])
        (message,) = self.bot.find("SOS: LMB-S HQ, LMB-T EDC")
        await self.desk.on_mesh("LMB-T EDC", "SOS! Need help. #0002")  # its repeat
        await self.desk.on_mesh("LMB-T EDC", "ack")  # a caller doesn't acknowledge
        self.assertEqual(self.desk.emergency.aware, [])
        self.assertEqual(self.bot.find("SOS: LMB-S HQ, LMB-T EDC"), [message])
        await self.press_on(1, "SOS: LMB-S HQ, LMB-T EDC", "ACK")
        self.assertEqual(self.sent, ["<Thomas> ACK #0002 I am aware"])
        await self.desk.on_mesh("Bob", "RESOLVED #0001 all OK")  # either id names it
        self.assertIsNone(self.desk.emergency)

    async def test_ack_with_nothing_open_is_shown_as_plain_text(self):
        await self.desk.on_mesh("LMB-T EDC", "ack")
        await self.desk.on_mesh("LMB-T EDC", "ACK #83d3 I am aware")  # a late one from a board: not shown
        self.assertEqual(len(self.bot.find("[LMB-T EDC] ack")), 1)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIsNone(self.desk.emergency)

    # --- SOS from Telegram ----------------------------------------------------------------------

    async def test_sos_button_asks_then_sends_under_the_pressers_first_name(self):
        await self.press(1, sos_desk.SOS)
        self.assertEqual(self.sent, [])
        (question,) = self.bot.find("<b>Thomas</b>, send an SOS?")
        answer = await self.press(1, self.bot.button(question, "Yes"), question)
        self.assertEqual(answer[1], "SOS sent.")
        emergency = self.desk.emergency
        self.assertEqual(self.sent, [f"<Thomas> SOS! Need help. #{emergency.incident_id}"])
        (message,) = self.bot.find("SOS: Thomas")
        self.assertIn("Sent from Telegram · on the radio 1×", self.bot.messages[message]["text"])
        self.assertNotIn(question, self.bot.messages)

    async def test_sos_is_repeated_until_someone_acknowledges(self):
        await self.send_sos()
        for _ in range(2):
            self.clock += sos_desk.RETRY_SECONDS + 1
            await self.desk.tick()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(set(self.sent)), 1)
        (message,) = self.bot.find("SOS: Thomas")
        self.assertIn("on the radio 3×", self.bot.messages[message]["text"])
        await self.press_on(2, "SOS: Thomas", "ACK")
        self.clock += sos_desk.RETRY_SECONDS + 1
        await self.desk.tick()
        self.assertEqual(len(self.sent), 4)  # the three sends and Anna's ACK

    async def test_sos_stops_after_five_sends(self):
        await self.send_sos()
        for _ in range(10):
            self.clock += sos_desk.RETRY_SECONDS + 1
            await self.desk.tick()
        self.assertEqual(len(self.sent), sos_desk.MAX_SENDS)

    async def test_no_second_sos_while_an_emergency_is_active(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        answer = await self.press(1, sos_desk.SOS)
        self.assertEqual(answer[1:], (sos_desk.ALREADY_ACTIVE, True))
        self.assertEqual(self.bot.find("send an SOS?"), [])
        self.assertEqual(self.sent, [])
        # After it's resolved a new one can be sent.
        await self.desk.on_mesh("LMB-S HQ", "RESOLVED #83d3 all OK")
        await self.send_sos()
        self.assertEqual(len(self.sent), 1)
        # Nor a second one by someone else from Telegram.
        answer = await self.press(2, sos_desk.SOS)
        self.assertTrue(answer[2])
        self.assertEqual(len(self.sent), 1)

    async def test_an_emergency_that_starts_while_the_question_is_open_wins(self):
        await self.press(1, sos_desk.SOS)
        (question,) = self.bot.find("send an SOS?")
        yes = self.bot.button(question, "Yes")
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        self.assertNotIn(question, self.bot.messages)  # the question is withdrawn
        answer = await self.press(1, yes, question)
        self.assertTrue(answer[2])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.desk.emergency.callers, ["LMB-S HQ"])

    async def test_the_caller_cannot_acknowledge_but_can_resolve(self):
        await self.send_sos()
        answer = await self.press_on(1, "SOS: Thomas", "ACK")
        self.assertTrue(answer[2])
        self.assertEqual(len(self.sent), 1)
        await self.press_on(1, "SOS: Thomas", "Resolved")
        (question,) = self.bot.find("emergency over?")
        await self.press(1, self.bot.button(question, "Yes"), question)
        self.assertIn("RESOLVED", self.sent[-1])
        self.assertIsNone(self.desk.emergency)

    async def test_radio_offline_is_said_to_the_presser(self):
        self.online = False
        answer = await self.send_sos()
        self.assertIn("radio is offline", answer[1])

    async def test_a_board_resolving_closes_the_sos_sent_from_telegram(self):
        await self.send_sos()
        emergency = self.desk.emergency
        await self.desk.on_mesh("LMB-S HQ", f"RESOLVED #{emergency.incident_id} all OK")
        self.assertIsNone(self.desk.emergency)
        self.clock += sos_desk.RETRY_SECONDS + 1
        await self.desk.tick()
        self.assertEqual(len(self.sent), 1)

    # --- Names ----------------------------------------------------------------------------------

    async def test_people_with_the_same_first_name_stay_apart(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        for user in (2, 4, 5, 2):
            await self.press_on(user, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(self.desk.emergency.aware, ["Anna", "Anna B", "Anna 2"])
        self.assertEqual(self.sent[1], "<Anna B> ACK #83d3 I am aware")
        # Names are kept across a restart.
        self.desk = self._desk()
        self.assertEqual(self.desk.name_for(SimpleNamespace(sender_id=4, sender_display_name="Anna Bianchi")), "Anna B")

    async def test_names_fit_what_a_board_can_read(self):
        name = lambda display, user=None: self.desk.name_for(SimpleNamespace(sender_id=user, sender_display_name=display))
        self.assertEqual(name("<An:na> Rossi"), "Anna")
        self.assertEqual(name("  "), "Telegram")
        self.assertEqual(name("x" * 40), "x" * 31)
        self.assertEqual(name("x" * 40 + " Yz", 8), "x" * 31)
        self.assertEqual(name("x" * 40 + " Yz", 9), "x" * 29 + " Y")
        # Someone who changes their first name gets the new one.
        self.assertEqual(name("Thomas Example", 1), "Thomas")
        self.assertEqual(name("Tom Example", 1), "Tom")

    # --- Typing, restarts -----------------------------------------------------------------------

    async def test_typing_sends_nothing_and_points_to_the_buttons_now_and_then(self):
        typed = TelegramMessageEvent(
            chat_id=-5, message_id=7, text="ack", text_source="text", is_from_bot=False,
            sender_display_name="Thomas Example", has_media=False, sender_id=1,
        )
        await self.desk.on_typed(typed)
        await self.desk.on_typed(typed)
        (hint,) = self.bot.find("Use the buttons")
        self.assertEqual(self.bot.messages[hint]["reply_to"], 7)
        self.assertEqual(self.bot.messages[hint]["buttons"], [(sos_desk.SOS_LABEL, sos_desk.SOS)])
        self.assertEqual(self.sent, [])
        # During an emergency the hint has no SOS button: there's nothing to send.
        await self.desk.on_mesh("Anna", "fire at the gate")
        self.clock += sos_desk.HINT_SECONDS + 1
        await self.desk.on_typed(typed)
        hints = self.bot.find("Use the buttons")
        self.assertEqual(len(hints), 2)
        self.assertEqual(self.bot.messages[max(hints)]["buttons"], [])

    async def test_buttons_keep_working_after_a_restart(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        self.desk = self._desk()
        await self.press_on(1, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(self.sent, ["<Thomas> ACK #83d3 I am aware"])

    async def test_state_from_the_version_with_several_incidents_is_read(self):
        self.path.write_text('{"panel_message_id": 7, "closed": ["0001"], "incidents": [{"key": "k", "sender": "A"}]}')
        desk = self._desk()
        self.assertIsNone(desk.emergency)
        await desk.on_mesh("A", "SOS! Need help. #0001")
        self.assertIsNone(desk.emergency)

    async def test_names_are_escaped(self):
        await self.desk.on_mesh("A&B", "smoke <b>everywhere</b>")
        (message,) = self.bot.find("SOS: A&amp;B")
        self.assertIn("smoke &lt;b&gt;everywhere&lt;/b&gt;", self.bot.messages[message]["text"])

    async def test_telegram_down_does_not_lose_the_radio_side(self):
        bot, self.bot = self.bot, None
        desk = self._desk()
        desk._bot = lambda: None
        with self.assertLogs("meshgram.sos_desk", level="WARNING"):
            await desk.on_mesh("Anna", "SOS! Need help. #0001")
        self.assertEqual(desk.emergency.callers, ["Anna"])
        self.bot = bot


if __name__ == "__main__":
    unittest.main()

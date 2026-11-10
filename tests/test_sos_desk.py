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

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
        self.next_id += 1
        self.messages[self.next_id] = {
            "text": text,
            "buttons": _buttons(reply_markup),
            "reply_to": kwargs.get("reply_to_message_id"),
        }
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
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


USERS = {1: ("Thomas Example", "LMB-T HQ"), 2: ("Anna Rossi", "Anna"), 3: ("Bob Stone", "Bob")}


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
            radio_name=lambda event: USERS[event.sender_id][1],
            now=lambda: self.clock,
        )
        desk._load()
        return desk

    async def press(self, user, data, message_id=0):
        self.presses += 1
        await self.desk.on_button(
            TelegramButtonEvent(
                chat_id=-5, message_id=message_id, data=data, query_id=f"q{self.presses}",
                sender_display_name=USERS[user][0], sender_id=user,
            )
        )
        return self.bot.answers[-1]

    async def press_on(self, user, fragment, label):
        (message_id,) = self.bot.find(fragment)
        return await self.press(user, self.bot.button(message_id, label), message_id)

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

    async def test_panel_survives_a_bot_that_may_not_pin(self):
        self.bot.fail_pin = True
        with self.assertLogs("meshgram.sos_desk", level="WARNING"):
            await self.desk.tick()
        self.assertEqual(len(self.bot.find("MeshSOS")), 1)

    # --- Alarms from the radio ------------------------------------------------------------------

    async def test_radio_alarm_shows_with_buttons_and_repeats_are_not_shown_again(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. bat 97% #83d3")
        (message,) = self.bot.find("SOS: LMB-S HQ")
        self.assertIn("SOS! Need help. bat 97%", self.bot.messages[message]["text"])
        self.assertNotIn("#83d3", self.bot.messages[message]["text"])
        self.assertIn("Nobody has acknowledged yet", self.bot.messages[message]["text"])
        self.assertEqual(len(self.bot.messages[message]["buttons"]), 2)
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. bat 96% #83d3")
        self.assertEqual(len(self.bot.messages), 1)

    async def test_ack_button_sends_an_ack_with_the_incident_id_in_the_pressers_name(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        answer = await self.press_on(1, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(self.sent, ["<LMB-T HQ> ACK #83d3 I am aware"])
        self.assertEqual(answer[1:], ("ACK sent as LMB-T HQ.", False))
        (message,) = self.bot.find("SOS: LMB-S HQ")
        self.assertIn("✅ Aware: LMB-T HQ", self.bot.messages[message]["text"])
        (notice,) = self.bot.find("is aware of")
        self.assertEqual(self.bot.messages[notice]["reply_to"], message)
        # A second press sends nothing more.
        answer = await self.press_on(1, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("already listed", answer[1])

    async def test_ack_from_the_radio_is_listed(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        await self.desk.on_mesh("LMB-T EDC", "ack")
        await self.desk.on_mesh("LMBot", "<Anna> ACK #83d3 I am aware")
        await self.desk.on_mesh("LMB-T EDC", "ok")  # again
        await self.desk.on_mesh("LMB-S HQ", "ack")  # their own
        (message,) = self.bot.find("SOS: LMB-S HQ")
        self.assertIn("✅ Aware: LMB-T EDC, Anna", self.bot.messages[message]["text"])
        self.assertEqual(self.sent, [])

    async def test_nobody_acknowledges_their_own_sos(self):
        await self.desk.on_mesh("Anna", "fire at the gate")
        answer = await self.press_on(2, "SOS: Anna", "ACK")
        self.assertEqual(self.sent, [])
        self.assertTrue(answer[2])

    async def test_resolved_asks_first_and_only_the_one_who_pressed_can_answer(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        await self.press_on(1, "SOS: LMB-S HQ", "Resolved")
        self.assertEqual(self.sent, [])
        (question,) = self.bot.find("is <b>LMB-S HQ</b>'s SOS over?")
        answer = await self.press(2, self.bot.button(question, "Yes"), question)
        self.assertEqual(answer[1:], ("This question is for Thomas.", True))
        self.assertEqual(self.sent, [])
        await self.press(1, self.bot.button(question, "Yes"), question)
        self.assertEqual(self.sent, ["<LMB-T HQ> RESOLVED #83d3 all OK"])
        self.assertNotIn(question, self.bot.messages)
        (message,) = self.bot.find("Resolved: LMB-S HQ")
        self.assertEqual(self.bot.messages[message]["buttons"], [])
        self.assertIn("Resolved by LMB-T HQ", self.bot.messages[message]["text"])
        self.assertEqual(self.desk.incidents, [])
        # A late repeat of the closed incident stays closed; its old button says so.
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        self.assertEqual(self.desk.incidents, [])

    async def test_cancel_and_timeout_send_nothing(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        await self.press_on(1, "SOS: LMB-S HQ", "Resolved")
        (question,) = self.bot.find("SOS over?")
        await self.press(1, self.bot.button(question, "Cancel"), question)
        self.assertNotIn(question, self.bot.messages)
        await self.press_on(1, "SOS: LMB-S HQ", "Resolved")
        (question,) = self.bot.find("SOS over?")
        data = self.bot.button(question, "Yes")
        self.clock += sos_desk.CONFIRM_SECONDS + 1
        await self.desk.tick()
        self.assertNotIn(question, self.bot.messages)
        answer = await self.press(1, data, question)
        self.assertIn("expired", answer[1])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.desk.incidents), 1)

    async def test_resolved_from_the_radio_closes_the_incident(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        (message,) = self.bot.find("SOS: LMB-S HQ")
        ack = self.bot.button(message, "ACK")
        await self.desk.on_mesh("LMB-S HQ", "RESOLVED #83d3 all OK")
        self.assertEqual(self.desk.incidents, [])
        self.assertTrue(self.bot.find("SOS is resolved (by LMB-S HQ)"))
        answer = await self.press(1, ack, message)
        self.assertEqual(answer[1:], ("This alarm is closed.", True))
        self.assertEqual(self.sent, [])

    async def test_ack_without_id_goes_to_the_newest_incident(self):
        await self.desk.on_mesh("Anna", "SOS! Need help. #0001")
        await self.desk.on_mesh("Bob", "fire at the gate")
        await self.desk.on_mesh("LMB-T EDC", "ack")
        self.assertEqual([incident.aware for incident in self.desk.incidents], [[], ["LMB-T EDC"]])
        # Bob's incident has no id: the ACK button sends one without.
        await self.press_on(1, "SOS: Bob", "ACK")
        self.assertEqual(self.sent, ["<LMB-T HQ> ACK I am aware"])

    async def test_more_from_the_same_sender_moves_the_incident_to_a_new_message(self):
        await self.desk.on_mesh("Anna", "fire at the gate")
        (first,) = self.bot.find("fire at the gate")
        await self.desk.on_mesh("Anna", "it is spreading")
        self.assertEqual(len(self.desk.incidents), 1)
        self.assertEqual(self.bot.messages[first]["buttons"], [])
        (second,) = self.bot.find("it is spreading")
        self.assertEqual(len(self.bot.messages[second]["buttons"]), 2)

    async def test_ack_with_nothing_open_is_shown_as_plain_text(self):
        await self.desk.on_mesh("LMB-T EDC", "ack")
        self.assertEqual(len(self.bot.find("[LMB-T EDC] ack")), 1)
        self.assertEqual(self.desk.incidents, [])

    # --- SOS from Telegram ----------------------------------------------------------------------

    async def _send_sos(self, user=1):
        await self.press(user, sos_desk.SOS)
        (question,) = self.bot.find("send an SOS as")
        return await self.press(user, self.bot.button(question, "Yes"), question)

    async def test_sos_button_asks_then_sends_in_the_pressers_name(self):
        await self.press(1, sos_desk.SOS)
        self.assertEqual(self.sent, [])
        (question,) = self.bot.find("send an SOS as <b>LMB-T HQ</b>?")
        answer = await self.press(1, self.bot.button(question, "Yes"), question)
        self.assertEqual(answer[1], "SOS sent as LMB-T HQ.")
        (incident,) = self.desk.incidents
        self.assertEqual(self.sent, [f"<LMB-T HQ> SOS! Need help. #{incident.incident_id}"])
        (message,) = self.bot.find("SOS: LMB-T HQ")
        self.assertIn("Sent from Telegram by Thomas Example · on the radio 1×", self.bot.messages[message]["text"])
        self.assertNotIn(question, self.bot.messages)

    async def test_sos_is_repeated_until_someone_acknowledges(self):
        await self._send_sos()
        for _ in range(2):
            self.clock += sos_desk.RETRY_SECONDS + 1
            await self.desk.tick()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(set(self.sent)), 1)
        (message,) = self.bot.find("SOS: LMB-T HQ")
        self.assertIn("on the radio 3×", self.bot.messages[message]["text"])
        await self.press_on(2, "SOS: LMB-T HQ", "ACK")
        self.clock += sos_desk.RETRY_SECONDS + 1
        await self.desk.tick()
        self.assertEqual(len(self.sent), 4)  # the three repeats and Anna's ACK

    async def test_sos_stops_after_five_sends(self):
        await self._send_sos()
        for _ in range(10):
            self.clock += sos_desk.RETRY_SECONDS + 1
            await self.desk.tick()
        self.assertEqual(len(self.sent), sos_desk.MAX_SENDS)

    async def test_one_open_sos_per_name(self):
        await self._send_sos()
        answer = await self.press(1, sos_desk.SOS)
        self.assertTrue(answer[2])
        self.assertIn("already open", answer[1])
        self.assertEqual(len(self.sent), 1)

    async def test_radio_offline_is_said_to_the_presser(self):
        self.online = False
        answer = await self._send_sos()
        self.assertIn("radio is offline", answer[1])

    async def test_own_board_resolving_closes_the_sos_sent_from_telegram(self):
        await self._send_sos()
        (incident,) = self.desk.incidents
        await self.desk.on_mesh("LMB-T HQ", f"RESOLVED #{incident.incident_id} all OK")
        self.assertEqual(self.desk.incidents, [])
        self.clock += sos_desk.RETRY_SECONDS + 1
        await self.desk.tick()
        self.assertEqual(len(self.sent), 1)

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
        self.clock += sos_desk.HINT_SECONDS + 1
        await self.desk.on_typed(typed)
        self.assertEqual(len(self.bot.find("Use the buttons")), 2)

    async def test_buttons_keep_working_after_a_restart(self):
        await self.desk.on_mesh("LMB-S HQ", "SOS! Need help. #83d3")
        self.desk = self._desk()
        await self.press_on(1, "SOS: LMB-S HQ", "ACK")
        self.assertEqual(self.sent, ["<LMB-T HQ> ACK #83d3 I am aware"])

    async def test_names_are_escaped(self):
        await self.desk.on_mesh("A&B", "smoke <b>everywhere</b>")
        (message,) = self.bot.find("SOS: A&amp;B")
        self.assertIn("smoke &lt;b&gt;everywhere&lt;/b&gt;", self.bot.messages[message]["text"])

    async def test_oldest_incident_makes_room(self):
        for index in range(sos_desk.MAX_OPEN + 1):
            await self.desk.on_mesh(f"Node{index}", "help")
        self.assertEqual(len(self.desk.incidents), sos_desk.MAX_OPEN)
        self.assertEqual(self.desk.incidents[0].sender, "Node1")
        self.assertEqual(len(self.bot.find("Closed here: Node0")), 1)

    async def test_telegram_down_does_not_lose_the_radio_side(self):
        bot, self.bot = self.bot, None
        desk = self._desk()
        desk._bot = lambda: None
        with self.assertLogs("meshgram.sos_desk", level="WARNING"):
            await desk.on_mesh("Anna", "SOS! Need help. #0001")
        self.assertEqual(len(desk.incidents), 1)
        self.bot = bot


if __name__ == "__main__":
    unittest.main()

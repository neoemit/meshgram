import base64
import csv
import hashlib
import hmac
import io
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from meshgram.config import MeshgramSettings, WebConfig
from meshgram.plugin import load_plugin_class
from meshgram.plugins.check_in import (
    DEFAULT_TEMPLATE,
    MAX_TITLE_LENGTH,
    CheckInPlugin,
    fill_template,
    pack_lines,
    schedule_from_settings,
    shorten,
    split_sender,
    summary_record,
)
from meshgram.plugins.check_in_scoring import Schedule
from meshgram.settings_schema import SettingsError, validate
from meshgram.status import StatusRegistry
from meshgram.text_utils import utf8_len
from meshgram.transport import MeshCoreTransport
from meshgram.types import MeshTextEvent, PluginContext
from meshgram.web import WebServer

try:
    from test_web_server import as_json, http  # unittest discover -s tests
except ImportError:
    from tests.test_web_server import as_json, http  # python -m unittest tests.test_check_in_plugin

PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
PUBLIC_HASH = hashlib.sha256(PUBLIC_SECRET).hexdigest()[:2]
KEY_A = "A1" * 32
KEY_B = "B2" * 32
OBSERVER_KEY = "D0" * 32
MONDAY_9AM = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc).timestamp()
LEADERBOARD = "/api/plugins/check_in/leaderboard"


def channel_packet(text: str, timestamp: int, secret: bytes = PUBLIC_SECRET) -> str:
    """A flooded GRP_TXT packet as MeshCore builds it: hash(1) || HMAC(2) || AES-128-ECB(timestamp, flags, text)."""
    from Crypto.Cipher import AES

    plain = timestamp.to_bytes(4, "little") + b"\x00" + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    ciphertext = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2]
    return f"{(5 << 2) | 1:02X}00" + (hashlib.sha256(secret).digest()[:1] + mac + ciphertext).hex()


def advert_packet(public_key: str, name: str, timestamp: int = 1000, route_type: int = 1) -> str:
    payload = bytes.fromhex(public_key) + timestamp.to_bytes(4, "little") + b"\x00" * 64 + bytes([0x81]) + name.encode()
    return f"{(4 << 2) | route_type:02X}00" + payload.hex()


def channel_event(sender: str, text: str, timestamp: int, channel: int = 0) -> MeshTextEvent:
    return MeshTextEvent(
        from_id=None,
        to_id=None,
        packet_id=f"mc-ch-{timestamp}",
        channel_index=channel,
        text=text,
        sender_label=sender,
        raw_packet={"type": "CHAN", "channel_idx": channel, "text": f"{sender}: {text}", "sender_timestamp": timestamp},
    )


class FakeTransport:
    def __init__(self):
        self.channel_slots = [
            {"index": 0, "name": "Public", "secret": PUBLIC_SECRET, "hash": PUBLIC_HASH},
            {"index": 1, "name": "", "secret": bytes(16), "hash": hashlib.sha256(bytes(16)).hexdigest()[:2]},
        ]
        self.local_short_name = "Base"
        self.device_self_info = {"name": "Base"}
        self.contacts = {KEY_A.lower(): {"adv_name": "LMB-T", "last_advert": 100}}
        self.listeners = []
        self.remote_listeners = []

    def add_rx_log_listener(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_rx_log_listener(self, listener):
        self.listeners.remove(listener)

    def add_remote_rx_log_listener(self, listener):
        if listener not in self.remote_listeners:
            self.remote_listeners.append(listener)

    def remove_remote_rx_log_listener(self, listener):
        self.remote_listeners.remove(listener)


class CheckInTestCase(unittest.IsolatedAsyncioTestCase):
    settings: dict = {}
    web_config = None

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"MESHGRAM_DATA_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data_dir = Path(tmp.name)

        self.web = None
        if self.web_config is not None:
            self.web = WebServer(self.web_config, StatusRegistry())
            await self.web.start()
            self.addAsyncCleanup(self.web.stop)
        self.sent = []
        self.plugin = CheckInPlugin(dict(self.settings))
        self.now = MONDAY_9AM
        self.plugin.clock = lambda: self.now
        await self.plugin.on_startup(self.context())
        self.addAsyncCleanup(self.plugin.on_shutdown)
        self.transport = FakeTransport()
        await self.plugin.on_mesh_connected(self.transport, self.context())

    def context(self) -> PluginContext:
        async def execute_actions(actions, plugin_name):
            self.sent.extend(actions)

        settings = MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="config.yaml")
        return PluginContext(
            settings=settings,
            telegram_group_id=-1,
            mesh_payload_limit=140,
            local_node_id=None,
            web=self.web,
            execute_actions=execute_actions,
        )

    async def check_in(self, sender: str, timestamp: int, text: str = "Hello #meshcoremonday!", channel: int = 0):
        return await self.plugin.on_mesh_message(channel_event(sender, text, timestamp, channel), self.context())

    async def observed(self, text: str, timestamp: int, secret: bytes = PUBLIC_SECRET):
        """An observer's packet as an MQTT observer plugin hands it on: the radio's transport decrypts it."""
        radio = MeshCoreTransport(MeshgramSettings(telegram_bot_token="t", telegram_group_id=-1, config_path="config.yaml"))
        radio._channels = {0: {"channel_name": "Public", "channel_secret": PUBLIC_SECRET}}
        radio.local_short_name = "Base"

        async def on_text(event):
            self.sent.extend(await self.plugin.on_mesh_message(event, self.context()))

        radio._on_text = on_text
        await radio.dispatch_remote_rx_log(
            {"payload": channel_packet(text, timestamp, secret), "observer_id": OBSERVER_KEY, "observer_name": "Hill", "network": "LetsMesh"}
        )


class CheckInTests(CheckInTestCase):
    def test_registered(self):
        self.assertIs(load_plugin_class("check-in"), CheckInPlugin)
        self.assertIs(load_plugin_class("check_in"), CheckInPlugin)

    async def test_replies_with_rank_streak_and_participants(self):
        actions = await self.check_in("LMB-T", 1000)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].channel_index, 0)
        self.assertEqual(
            actions[0].text,
            "📟 MeshCore Monday\n🏅 LMB-T is #1 this week\n🔥 Streak: 1\n👥 1 participant",
        )
        actions = await self.check_in("Zed", 1001, "#MESHCOREMONDAY")
        self.assertIn("🏅 Zed is #2 this week", actions[0].text)
        self.assertIn("👥 2 participants", actions[0].text)
        # Asking again later: the same rank, the current count.
        self.now += 60
        actions = await self.check_in("LMB-T", 1002)
        self.assertIn("🏅 LMB-T is #1", actions[0].text)
        self.assertIn("👥 2 participants", actions[0].text)

        board = await self.plugin.leaderboard()
        entries = {entry["name"]: entry for entry in board["leaderboard"]}
        # LMB-T's name is a contact's: it's identified by that node's key.
        self.assertEqual((entries["LMB-T"]["id"], entries["LMB-T"]["hash"], entries["LMB-T"]["resolved"]), (KEY_A, "A1", True))
        self.assertEqual((entries["Zed"]["id"], entries["Zed"]["resolved"]), (None, False))
        self.assertEqual(board["participants_this_period"], 2)
        self.assertEqual((entries["Zed"]["checked_in_this_period"], entries["Zed"]["period_rank"]), (True, 2))
        self.assertTrue(board["window"]["open"])
        self.assertEqual(
            (board["frequency"], board["check_in_day"], board["period"]), ("weekly", "Monday", "2026-10-12")
        )
        # The older names, for leaderboard sites made before daily check-ins.
        self.assertEqual((board["week"], board["check_in_days"], board["participants_this_week"]), ("2026-10-12", ["Monday"], 2))
        self.assertEqual((entries["Zed"]["checked_in_this_week"], entries["Zed"]["week_rank"]), (True, 2))

    async def test_leaderboard_says_where_to_check_in(self):
        self.assertEqual((await self.plugin.leaderboard())["channel"], {"name": "Public", "kind": "public"})
        local = hashlib.sha256(b"#local").digest()[:16]
        self.transport.channel_slots[1] = {"index": 1, "name": "#local", "secret": local, "hash": "x"}
        await self.plugin.apply_settings({"channel": 1})
        self.assertEqual((await self.plugin.leaderboard())["channel"], {"name": "#local", "kind": "hashtag"})
        # An empty slot, or a radio that hasn't been read: nothing to show.
        await self.plugin.apply_settings({"channel": 2})
        self.assertIsNone((await self.plugin.leaderboard())["channel"])

    async def test_streak_over_weeks(self):
        self.now = MONDAY_9AM - 7 * 86400
        await self.check_in("LMB-T", 900)
        self.now = MONDAY_9AM
        actions = await self.check_in("LMB-T", 1000)
        self.assertIn("🔥 Streak: 2\n", actions[0].text)
        # Tuesday: the streak holds until next Monday passes without a check-in.
        self.now = MONDAY_9AM + 86400
        board = await self.plugin.leaderboard()
        self.assertEqual((board["leaderboard"][0]["streak"], board["window"]["open"]), (2, False))
        self.now = MONDAY_9AM + 8 * 86400
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"][0]["streak"], 0)

    async def test_what_isnt_a_check_in(self):
        self.assertEqual(await self.check_in("LMB-T", 1, "hello"), [])
        self.assertEqual(await self.check_in("LMB-T", 2, channel=1), [])
        dm = channel_event("LMB-T", "#MeshCoreMonday", 3)
        dm.to_id, dm.channel_index = "abc", -1
        self.assertEqual(await self.plugin.on_mesh_message(dm, self.context()), [])
        nameless = channel_event("ch0", "#MeshCoreMonday", 4)
        nameless.raw_packet["text"] = "#MeshCoreMonday"
        self.assertEqual(await self.plugin.on_mesh_message(nameless, self.context()), [])
        self.now = MONDAY_9AM + 86400  # Tuesday
        self.assertEqual(await self.check_in("LMB-T", 5), [])
        self.assertEqual((await self.plugin.participants())["participants"], [])

    async def test_quiet_time_still_counts(self):
        await self.check_in("LMB-T", 1000)
        self.now += 10
        self.assertEqual(await self.check_in("LMB-T", 1001), [])
        participant = (await self.plugin.participants())["participants"][0]
        self.assertEqual(len(await self.plugin.check_ins(participant["id"])), 2)

    async def test_check_ins_observers_heard(self):
        await self.observed("Faraway: #MeshCoreMonday from afar", 2000)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Faraway is #1", self.sent[0].text)
        # Our radio hears the same message later: already counted and answered.
        self.assertEqual(await self.check_in("Faraway", 2000, "#MeshCoreMonday from afar"), [])
        # Our own reply, heard by an observer, and another channel's message don't count.
        await self.observed("Base: #MeshCoreMonday", 2001)
        await self.observed("Other: #MeshCoreMonday", 2002, secret=bytes(range(16)))
        self.assertEqual(len(self.sent), 1)
        participants = (await self.plugin.participants())["participants"]
        self.assertEqual([p["name"] for p in participants], ["Faraway"])
        log = await self.plugin.check_ins(participants[0]["id"])
        self.assertEqual((log[0]["source"], log[0]["observer_id"]), ("observer", OBSERVER_KEY))

    async def test_name_only_participant_linked_by_an_advert(self):
        await self.check_in("Newbie", 1000)
        self.assertFalse((await self.plugin.leaderboard())["leaderboard"][0]["resolved"])
        await self.transport.remote_listeners[0]({"payload": advert_packet(KEY_B, "Newbie"), "observer_id": OBSERVER_KEY})
        await self.plugin._flush_nodes()
        entry = (await self.plugin.leaderboard())["leaderboard"][0]
        self.assertEqual((entry["id"], entry["resolved"]), (KEY_B, True))

    async def test_first_flood_advert_links_a_name_other_nodes_had(self):
        # The radio's contacts know two nodes called "Twin": a check-in from "Twin" is unverified.
        self.transport.contacts = {KEY_A.lower(): {"adv_name": "Twin", "last_advert": 100}, KEY_B.lower(): {"adv_name": "Twin", "last_advert": 90}}
        self.plugin._refresh_from_radio()
        await self.plugin._flush_nodes()
        await self.check_in("Twin", 1000)
        self.assertFalse((await self.plugin.leaderboard())["leaderboard"][0]["resolved"])
        # A zero-hop (direct) advert doesn't decide it; the first flood advert does, whoever heard it.
        await self.transport.listeners[0]({"payload": advert_packet(KEY_A, "Twin", 2000, route_type=2)})
        await self.plugin._flush_nodes()
        self.assertFalse((await self.plugin.leaderboard())["leaderboard"][0]["resolved"])
        await self.transport.remote_listeners[0]({"payload": advert_packet(KEY_B, "Twin", 2001), "observer_id": OBSERVER_KEY})
        await self.transport.listeners[0]({"payload": advert_packet(KEY_A, "Twin", 2002)})
        await self.plugin._flush_nodes()
        entry = (await self.plugin.leaderboard())["leaderboard"][0]
        self.assertEqual((entry["id"], entry["resolved"]), (KEY_B, True))

    async def test_flood_advert_from_the_live_feed_links_a_name(self):
        self.transport.contacts = {KEY_A.lower(): {"adv_name": "Twin", "last_advert": 100}, KEY_B.lower(): {"adv_name": "Twin", "last_advert": 90}}
        self.plugin._refresh_from_radio()
        await self.check_in("Twin", 1000)
        await self.transport.remote_listeners[0](
            {
                "decoded": {"hash": "00" * 8, "payload_type": 4, "route_type": 1, "path": []},
                "observer_id": OBSERVER_KEY,
                "source_node": {"public_key": KEY_A, "name": "Twin"},
            }
        )
        await self.plugin._flush_nodes()
        entry = (await self.plugin.leaderboard())["leaderboard"][0]
        self.assertEqual((entry["id"], entry["resolved"]), (KEY_A, True))

    async def test_longest_name_and_title_fit_in_one_message(self):
        # MeshCore node names are at most 31 bytes.
        name = "A very long node name, 31 bytes"
        await self.plugin.apply_settings({"title": "T" * MAX_TITLE_LENGTH})
        actions = await self.check_in(name, 1000)
        self.assertEqual(len(actions), 1)
        self.assertLessEqual(utf8_len(actions[0].text), 120)
        self.assertEqual(
            actions[0].text,
            DEFAULT_TEMPLATE.format(
                title="T" * MAX_TITLE_LENGTH, name=name, rank=1, this_period="this week", streak=1, participants_label="1 participant"
            ),
        )

    async def test_reply_too_long_shortens_the_name_and_title(self):
        await self.plugin.apply_settings({"title": "🌍" * MAX_TITLE_LENGTH})
        actions = await self.check_in("📡📡📡📡📡📡📡 Hilltop", 1000)
        self.assertEqual(len(actions), 1)
        self.assertLessEqual(utf8_len(actions[0].text), 120)
        lines = actions[0].text.splitlines()
        self.assertTrue(lines[0].startswith("📟 🌍") and lines[0].endswith("…"), lines[0])
        self.assertTrue(lines[1].startswith("🏅 📡📡") and lines[1].endswith("… is #1 this week"), lines[1])

    async def test_reply_that_cant_fit_is_split_between_lines(self):
        template = "{title} {keyword}\n" + "x" * 100 + "\n{name} is #{rank}"
        await self.plugin.apply_settings({"reply_template": template})
        actions = await self.check_in("LMB-T", 1000)
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(utf8_len(action.text) <= 120 for action in actions))
        self.assertEqual((actions[0].delay_ms, actions[1].delay_ms), (0, 2500))
        self.assertEqual("\n".join(action.text for action in actions), template.format(
            title="MeshCore Monday", keyword="#MeshCoreMonday", name="LMB-T", rank=1
        ))

    async def test_bad_template_falls_back_to_the_default(self):
        await self.plugin.apply_settings({"reply_template": "{nope} {name}"})
        with self.assertLogs("meshgram.plugins.check_in", "WARNING"):
            actions = await self.check_in("LMB-T", 1000)
        self.assertTrue(actions[0].text.startswith("📟 MeshCore Monday"))

    async def test_settings_change_live_but_the_database(self):
        self.assertTrue(await self.plugin.apply_settings({"keyword": "#Roll", "frequency": "daily", "title": "Roll call"}))
        self.now = MONDAY_9AM + 86400  # any day now
        actions = await self.check_in("LMB-T", 1000, "#roll")
        self.assertTrue(actions[0].text.startswith("📟 Roll call"))
        self.assertFalse(await self.plugin.apply_settings({"db_path": "other.sqlite3"}))

    async def test_weekly_on_another_day(self):
        await self.plugin.apply_settings({"day": 2})  # Wednesday
        self.assertEqual(await self.check_in("LMB-T", 1000), [])  # Monday
        board = await self.plugin.leaderboard()
        self.assertEqual((board["check_in_day"], board["window"]["open"]), ("Wednesday", False))
        self.assertEqual(board["window"]["opens_at"], "2026-10-14T00:00:00Z")
        self.now = MONDAY_9AM + 2 * 86400
        actions = await self.check_in("LMB-T", 1001)
        self.assertIn("is #1 this week", actions[0].text)
        # Next Monday: the streak holds until Wednesday passes.
        self.now = MONDAY_9AM + 7 * 86400
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"][0]["streak"], 1)
        self.now = MONDAY_9AM + 10 * 86400
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"][0]["streak"], 0)

    async def test_shutdown_lets_go(self):
        await self.plugin.on_shutdown()
        self.assertEqual((self.transport.listeners, self.transport.remote_listeners), ([], []))
        self.assertIsNone(self.plugin.store)


class DailyTests(CheckInTestCase):
    settings = {"frequency": "daily", "keyword": "#Daily", "title": "Daily roll call"}

    async def test_every_day_counts_with_a_streak_of_days(self):
        for day in range(3):
            self.now = MONDAY_9AM + day * 86400
            actions = await self.check_in("LMB-T", 1000 + day, "#daily")
        self.assertEqual(
            actions[0].text, "📟 Daily roll call\n🏅 LMB-T is #1 today\n🔥 Streak: 3\n👥 1 participant"
        )
        # Once a day: a second check-in the same day is the same rank and streak.
        self.now += 3600
        actions = await self.check_in("LMB-T", 2000, "#daily")
        self.assertIn("🔥 Streak: 3\n", actions[0].text)
        await self.check_in("Zed", 2001, "#daily")

        board = await self.plugin.leaderboard()
        self.assertEqual(
            (board["frequency"], board["check_in_day"], board["period"], board["participants_this_period"]),
            ("daily", None, "2026-10-14", 2),
        )
        self.assertEqual(
            (board["window"]["open"], board["window"]["closes_at"]), (True, "2026-10-15T00:00:00Z")
        )
        self.assertEqual(len(board["check_in_days"]), 7)
        entries = {entry["name"]: entry for entry in board["leaderboard"]}
        self.assertEqual((entries["LMB-T"]["streak"], entries["LMB-T"]["total_check_ins"]), (3, 3))
        self.assertEqual(entries["Zed"]["period_rank"], 2)

        # The next day the streak holds; a day without a check-in breaks it.
        self.now = MONDAY_9AM + 3 * 86400
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"][0]["streak"], 3)
        self.now = MONDAY_9AM + 4 * 86400
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"][0]["streak"], 0)

    async def test_streak_label_and_switching_to_weekly(self):
        await self.plugin.apply_settings({**self.settings, "reply_template": "{name}: {streak_label}"})
        await self.check_in("LMB-T", 1000, "#daily")
        self.now += 86400
        actions = await self.check_in("LMB-T", 1001, "#daily")
        self.assertEqual(actions[0].text, "LMB-T: 2 days")
        # The same check-ins, counted weekly: one week (Monday and Tuesday are in it).
        await self.plugin.apply_settings({"keyword": "#Daily", "day": 1})
        entry = (await self.plugin.leaderboard())["leaderboard"][0]
        self.assertEqual((entry["streak"], entry["total_check_ins"], entry["checked_in_this_period"]), (1, 1, True))

    async def test_manual_days(self):
        participant = await self.plugin.create_participant("Manual", None)
        await self.plugin.set_streak(participant, 5, None)
        data = await self.plugin.participants()
        manual = data["participants"][0]
        self.assertEqual((manual["streak"], manual["periods"][0], len(manual["periods"])), (5, "2026-10-12", 5))
        await self.plugin.set_period(participant, date(2026, 10, 10), False)
        self.assertEqual((await self.plugin.participants())["participants"][0]["streak"], 2)


class SettingsTests(unittest.TestCase):
    def test_schedule(self):
        self.assertEqual(schedule_from_settings({}), Schedule("weekly", 0))
        self.assertEqual(schedule_from_settings({"frequency": "daily"}), Schedule("daily"))
        self.assertEqual(schedule_from_settings({"frequency": "Weekly", "day": "fri"}), Schedule("weekly", 4))
        self.assertEqual(schedule_from_settings({"day": 6}), Schedule("weekly", 6))
        with self.assertLogs("meshgram.plugins.check_in", "WARNING"):
            self.assertEqual(schedule_from_settings({"frequency": "monthly", "day": 9}), Schedule("weekly", 0))

    def test_old_days_setting(self):
        self.assertEqual(schedule_from_settings({"days": [2]}), Schedule("weekly", 2))
        self.assertEqual(schedule_from_settings({"days": "sat"}), Schedule("weekly", 5))
        # "day" wins.
        self.assertEqual(schedule_from_settings({"days": [2], "day": 3}), Schedule("weekly", 3))
        # Several days, or any day: weekly check-ins have one day now.
        with self.assertLogs("meshgram.plugins.check_in", "WARNING"):
            self.assertEqual(schedule_from_settings({"days": [4, 1]}), Schedule("weekly", 1))
        with self.assertLogs("meshgram.plugins.check_in", "WARNING"):
            self.assertEqual(schedule_from_settings({"days": []}), Schedule("weekly", 0))

    def test_settings_schema(self):
        schema = CheckInPlugin.settings_schema
        validate({"frequency": "daily"}, schema)
        validate({"frequency": "weekly", "day": 6}, schema)
        for settings in ({"frequency": "monthly"}, {"day": 7}, {"day": "monday"}):
            with self.assertRaises(SettingsError):
                validate(settings, schema)


class HelperTests(unittest.TestCase):
    def test_split_sender(self):
        self.assertEqual(split_sender("LMB-T: hi: there"), ("LMB-T", "hi: there"))
        self.assertEqual(split_sender("no sender"), (None, "no sender"))
        self.assertEqual(split_sender("x" * 40 + ": hi")[0], None)

    def test_default_reply_fits_with_three_digit_numbers(self):
        values = {
            "title": "T" * MAX_TITLE_LENGTH,
            "name": "N" * 31,
            "rank": 999,
            "this_period": "this week",
            "streak": 999,
            "participants_label": "999 participants",
        }
        text = fill_template(DEFAULT_TEMPLATE, values, 120)
        self.assertEqual((text, utf8_len(text)), (DEFAULT_TEMPLATE.format_map(values), 119))
        # A thousand participants: the longer of the name and title loses characters (the name, here).
        values.update(rank=1000, participants_label="1000 participants")
        text = fill_template(DEFAULT_TEMPLATE, values, 120)
        self.assertEqual(utf8_len(text), 120)
        self.assertIn("🏅 " + "N" * 27 + "… is #1000", text)
        self.assertIn("📟 " + "T" * MAX_TITLE_LENGTH + "\n", text)
        self.assertEqual(fill_template(DEFAULT_TEMPLATE, values), DEFAULT_TEMPLATE.format_map(values))

    def test_shorten(self):
        self.assertEqual(shorten("Hilltop", 7), "Hilltop")
        self.assertEqual(shorten("Hilltop", 6), "Hillt…")
        self.assertEqual(shorten("Hill top", 6), "Hill…")
        self.assertEqual(shorten("ab‍cd", 4), "ab…")

    def test_pack_lines(self):
        self.assertEqual(pack_lines("aaa\nbbb\nccc", 7), ["aaa\nbbb", "ccc"])
        self.assertEqual(pack_lines("a" * 10, 4), ["aaaa", "aaaa", "aa"])

    def test_summary_records(self):
        default = date(2026, 10, 5)
        weekly = Schedule()
        raw = {"Public_Key": "a1b2", "Name": "X", "streak": "15", "total": 20, "last_checkin": "2026-10-07T10:00Z"}
        record = summary_record(1, raw, default, weekly)
        self.assertEqual((record.node_id, record.streak, record.total, record.last_period), ("a1b2", 15, 20, date(2026, 10, 5)))
        self.assertEqual(summary_record(1, raw, default, Schedule("daily")).last_period, date(2026, 10, 7))
        self.assertEqual(summary_record(2, {"name": "Y", "streak": 0}, default, weekly).last_period, default)
        for raw, message in [
            ({"streak": 1}, "needs a node key or a name"),
            ({"name": "Y"}, "needs a streak"),
            ({"name": "Y", "streak": 1.5}, "whole number"),
            ({"name": "Y", "streak": 3, "total": 2}, "smaller than the streak"),
            ({"node_id": "xyz", "streak": 1}, "isn't a hex public key"),
            ({"name": "Y", "streak": 1, "last_check_in": "soon"}, "isn't a date"),
        ]:
            with self.assertRaisesRegex(ValueError, message):
                summary_record(3, raw, default, weekly)


class LeaderboardAccessTests(CheckInTestCase):
    web_config = WebConfig(port=0, password="secret", title="LMB Meshgram")
    settings = {"api_token": "tok"}

    async def get(self, path, headers=None):
        status, response_headers, body = await http(self.web.port, "GET", path, headers=headers)
        return status, as_json(body), response_headers

    async def test_token_or_password(self):
        await self.check_in("LMB-T", 1000)
        status, _, headers = await self.get(LEADERBOARD)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["www-authenticate"])
        self.assertEqual((await self.get(LEADERBOARD, {"Authorization": "Bearer nope"}))[0], 401)
        status, board, _ = await self.get(LEADERBOARD, {"Authorization": "Bearer tok"})
        self.assertEqual((status, board["leaderboard"][0]["name"]), (200, "LMB-T"))
        self.assertEqual(board["meshgram_title"], "LMB Meshgram")
        basic = "Basic " + base64.b64encode(b"x:secret").decode()
        self.assertEqual((await self.get(LEADERBOARD, {"Authorization": basic}))[0], 200)
        # The token opens the leaderboard only, not the rest of the web app.
        self.assertEqual((await self.get("/api/plugins/check_in/participants", {"Authorization": "Bearer tok"}))[0], 401)
        # Gone with the plugin.
        await self.plugin.on_shutdown()
        self.assertEqual((await self.get(LEADERBOARD, {"Authorization": basic}))[0], 404)


class LeaderboardWithoutPasswordTests(CheckInTestCase):
    web_config = WebConfig(port=0)

    async def test_open_unless_a_token_is_set(self):
        status, _, _ = await http(self.web.port, "GET", LEADERBOARD)
        self.assertEqual(status, 200)
        await self.plugin.apply_settings({"api_token": "tok"})
        self.assertEqual((await http(self.web.port, "GET", LEADERBOARD))[0], 401)
        self.assertEqual((await http(self.web.port, "GET", LEADERBOARD, headers={"Authorization": "Bearer tok"}))[0], 200)


class ManagementApiTests(CheckInTestCase):
    web_config = WebConfig(port=0)

    async def call(self, method, path, body=None):
        if body is None and method in {"POST", "PUT", "PATCH"}:
            body = {}
        status, headers, payload = await http(self.web.port, method, f"/api/plugins/check_in{path}", body=body)
        return status, (as_json(payload) if headers.get("content-type", "").startswith("application/json") else payload)

    async def test_manage_participants(self):
        events = self.web.events.subscribe()
        await self.check_in("Zed", 1000)
        self.assertEqual(events.get_nowait(), {"type": "control", "what": "check_in"})

        status, created = await self.call("POST", "/participants", {"name": "Manual", "node_id": KEY_B.lower()})
        self.assertEqual(status, 201)
        pid = created["id"]
        self.assertEqual((await self.call("PUT", f"/participants/{pid}/periods/2026-10-06"))[0], 204)
        self.assertEqual((await self.call("PUT", f"/participants/{pid}/streak", {"streak": 4, "as_of": "2026-10-12"}))[0], 204)
        status, data = await self.call("GET", "/participants")
        manual = next(p for p in data["participants"] if p["id"] == pid)
        self.assertEqual((manual["node_id"], manual["streak"], manual["periods"][0]), (KEY_B, 4, "2026-10-12"))
        self.assertEqual((data["frequency"], data["period"]), ("weekly", "2026-10-12"))
        self.assertEqual((await self.call("DELETE", f"/participants/{pid}/periods/2026-10-12"))[0], 204)

        # Linking Zed to Manual's key merges the two.
        zed = next(p for p in data["participants"] if p["name"] == "Zed")
        status, merged = await self.call("PATCH", f"/participants/{zed['id']}", {"node_id": KEY_B})
        self.assertEqual((status, merged["id"]), (200, pid))
        status, rows = await self.call("GET", f"/participants/{pid}/check-ins")
        self.assertIn("radio", {row["source"] for row in rows["check_ins"]})

        self.assertEqual((await self.call("PATCH", f"/participants/{pid}", {"node_id": "nope"}))[0], 400)
        self.assertEqual((await self.call("PATCH", f"/participants/{pid}", {"rank": 1}))[0], 400)
        self.assertEqual((await self.call("PUT", f"/participants/{pid}/periods/someday"))[0], 400)
        self.assertEqual((await self.call("DELETE", f"/participants/{pid}"))[0], 204)
        self.assertEqual((await self.call("DELETE", f"/participants/{pid}"))[0], 404)

    async def test_import_and_export(self):
        records = [
            {"node_id": KEY_A, "streak": 15, "total": 22, "last_check_in": "2026-10-05"},
            {"name": "Faraway", "streak": 3},
            {"name": "", "streak": 1},
        ]
        status, preview = await self.call("POST", "/import", {"records": records, "dry_run": True})
        self.assertEqual((status, preview["saved"]), (200, False))
        self.assertEqual(preview["errors"], [{"row": 3, "message": "needs a node key or a name"}])
        status, result = await self.call("POST", "/import", {"records": records[:2], "as_of": "2026-10-07"})
        self.assertEqual((result["saved"], result["created"], result["as_of"]), (True, 2, "2026-10-05"))

        board = (await self.plugin.leaderboard())["leaderboard"]
        self.assertEqual([(e["name"], e["streak"], e["total_check_ins"]) for e in board], [("LMB-T", 15, 22), ("Faraway", 3, 3)])

        # Checking in on Monday continues the imported streak.
        actions = await self.check_in("LMB-T", 1000)
        self.assertIn("🔥 Streak: 16\n", actions[0].text)

        status, payload = await self.call("GET", "/export?format=csv")
        rows = list(csv.DictReader(io.StringIO(payload.decode())))
        self.assertEqual((rows[0]["node_id"], rows[0]["streak"], rows[0]["total"], rows[0]["last_check_in"]), (KEY_A, "16", "23", "2026-10-12"))
        status, exported = await self.call("GET", "/export")
        self.assertEqual(len(exported["records"]), 2)

        # What's exported imports again unchanged.
        status, result = await self.call("POST", "/import", {"records": exported["records"], "mode": "replace"})
        self.assertTrue(result["saved"])
        again = (await self.plugin.leaderboard())["leaderboard"]
        self.assertEqual([(e["name"], e["streak"], e["total_check_ins"]) for e in again], [("LMB-T", 16, 23), ("Faraway", 3, 3)])

        self.assertEqual((await self.call("POST", "/import", {"records": "x"}))[0], 400)
        self.assertEqual((await self.call("POST", "/import", {"records": [], "mode": "wipe"}))[0], 400)

    async def test_backup_clear_and_restore(self):
        await self.check_in("LMB-T", 1000)
        await self.check_in("Zed", 1001)
        status, created = await self.call("POST", "/participants", {"name": "Manual"})
        await self.call("PUT", f"/participants/{created['id']}/streak", {"streak": 3, "as_of": "2026-10-12"})
        before = (await self.plugin.leaderboard())["leaderboard"]

        status, backup = await self.call("GET", "/backup")
        self.assertEqual((status, backup["format"], backup["version"]), (200, "meshgram-check-ins", 2))
        self.assertEqual((backup["frequency"], backup["participants"][0]["check_ins"][0]["day"]), ("weekly", "2026-10-12"))
        self.assertEqual(sum(len(p["check_ins"]) for p in backup["participants"]), 5)

        events = self.web.events.subscribe()
        status, cleared = await self.call("DELETE", "/participants")
        self.assertEqual((status, cleared), (200, {"participants": 3, "check_ins": 5}))
        self.assertEqual(events.get_nowait(), {"type": "control", "what": "check_in"})
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"], [])

        status, preview = await self.call("POST", "/restore", {"backup": backup, "dry_run": True})
        self.assertEqual((status, preview["created"], preview["check_ins"], preview["saved"]), (200, 3, 5, False))
        status, result = await self.call("POST", "/restore", {"backup": backup, "mode": "replace"})
        self.assertEqual((result["saved"], result["errors"]), (True, []))
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"], before)
        # The same check-in heard again isn't counted twice.
        self.now += 60
        await self.check_in("LMB-T", 1000)
        self.assertEqual((await self.plugin.leaderboard())["leaderboard"], before)

        broken = {**backup, "participants": [*backup["participants"], {"name": "", "check_ins": []}]}
        status, result = await self.call("POST", "/restore", {"backup": broken})
        self.assertEqual((result["saved"], result["errors"]), (False, [{"row": 4, "message": "name must be 1 to 64 characters"}]))
        status, result = await self.call("POST", "/restore", {"backup": {"records": []}})
        self.assertEqual(status, 400)
        self.assertEqual((await self.call("POST", "/restore", {"backup": {**backup, "version": 3}}))[0], 400)
        self.assertEqual((await self.call("POST", "/restore", {"backup": backup, "mode": "wipe"}))[0], 400)

    async def test_restore_takes_a_large_backup(self):
        check_ins = [
            {"week": "2026-10-12", "received_at": MONDAY_9AM + n, "source": "radio", "sender_name": "Big", "text": "#MeshCoreMonday " + "x" * 200, "message_key": f"k{n}"}
            for n in range(2000)
        ]
        backup = {"format": "meshgram-check-ins", "version": 1, "participants": [{"node_id": None, "name": "Big", "check_ins": check_ins}]}
        status, result = await self.call("POST", "/restore", {"backup": backup})
        self.assertEqual((status, result["check_ins"]), (200, 2000))

    async def test_restore_a_version_1_backup(self):
        # Check-ins by week: each gets the day it was received on, in the backup's time zone.
        sunday_night = MONDAY_9AM - 10 * 3600  # 23:00 UTC on Sunday 11 October: Monday in Rome
        backup = {
            "format": "meshgram-check-ins",
            "version": 1,
            "timezone": "Europe/Rome",
            "participants": [
                {"node_id": None, "name": "Old", "check_ins": [{"week": "2026-10-12", "received_at": sunday_night, "source": "radio"}]}
            ],
        }
        status, result = await self.call("POST", "/restore", {"backup": backup})
        self.assertEqual((status, result["check_ins"]), (200, 1))
        participant = (await self.plugin.participants())["participants"][0]
        self.assertEqual((await self.plugin.check_ins(participant["id"]))[0]["day"], "2026-10-12")
        self.assertTrue(participant["checked_in_this_period"])

    async def test_node_search(self):
        await self.plugin._flush_nodes()
        status, data = await self.call("GET", "/nodes?q=lmb")
        self.assertEqual((status, [node["node_id"] for node in data["nodes"]]), (200, [KEY_A]))


if __name__ == "__main__":
    unittest.main()

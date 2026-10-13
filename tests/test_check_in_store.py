import sqlite3
import tempfile
import unittest
import zoneinfo
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from meshgram.plugins.check_in_scoring import Schedule
from meshgram.plugins.check_in_store import (
    SCHEMA_VERSION,
    SOURCE_IMPORT,
    SOURCE_MANUAL,
    SOURCE_OBSERVER,
    SOURCE_RADIO,
    BackupCheckIn,
    BackupParticipant,
    CheckInError,
    CheckInRecord,
    CheckInStore,
    CheckInStoreError,
    NodeSighting,
    SummaryRecord,
)

MON = date(2026, 10, 12)
KEY_A = "A1" * 32
KEY_B = "B2" * 32
KEY_C = "C3" * 32
NOW = 1_800_000_000.0
WEEKLY = Schedule("weekly", 0)
DAILY = Schedule("daily")


def place(day: date) -> float:
    return datetime.combine(day, time(12), tzinfo=timezone.utc).timestamp()


def record(sender: str, key: str = None, day: date = MON, source: str = SOURCE_RADIO, at: float = NOW) -> CheckInRecord:
    return CheckInRecord(sender, "#MeshCoreMonday", at, day, source, message_key=key)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "check_in.sqlite3"
        self.store = CheckInStore(self.path)
        self.store.open()
        self.addCleanup(self.store.close)

    def people(self, schedule: Schedule = WEEKLY):
        return {h.name: h for h in self.store.histories(schedule)}


class CheckInTests(StoreTestCase):
    def test_one_message_heard_twice_is_stored_once(self):
        first = self.store.record_check_in(record("LMB-T", key="k1"))
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.record_check_in(record("LMB-T", key="k1", source=SOURCE_OBSERVER)))
        self.assertEqual(self.store.record_check_in(record("LMB-T", key="k2", at=NOW + 60)), first)
        history = self.people()["LMB-T"]
        self.assertEqual((history.periods, history.last_check_in), ({MON: NOW}, NOW + 60))
        self.assertEqual(len(self.store.check_ins(first)), 2)

    def test_names_resolve_to_the_one_node_that_has_them(self):
        self.store.upsert_nodes([NodeSighting(KEY_A, "Alpha", 100), NodeSighting(KEY_B, "Twin"), NodeSighting(KEY_C, "Twin")], NOW)
        self.store.record_check_in(record("Alpha", "k1"))
        self.store.record_check_in(record("Twin", "k2"))  # two nodes are called that: name only
        people = self.people()
        self.assertEqual((people["Alpha"].node_id, people["Twin"].node_id), (KEY_A, None))

    def test_name_only_participant_is_linked_by_a_later_advert(self):
        pid = self.store.record_check_in(record("Newbie", "k1"))
        self.assertIsNone(self.people()["Newbie"].node_id)
        self.assertEqual(self.store.upsert_nodes([NodeSighting(KEY_A, "Newbie", 100)], NOW), 1)
        self.assertEqual(self.people()["Newbie"].node_id, KEY_A)
        # Its next check-in is the same participant.
        self.assertEqual(self.store.record_check_in(record("Newbie", "k2", day=MON + timedelta(weeks=1))), pid)

    def test_first_flood_advert_links_a_name_several_nodes_had(self):
        self.store.upsert_nodes([NodeSighting(KEY_B, "Twin", 100), NodeSighting(KEY_C, "Twin", 100)], NOW)
        pid = self.store.record_check_in(record("Twin", "k1"))
        self.assertIsNone(self.people()["Twin"].node_id)
        # Heard again in the contact list: still two candidates.
        self.assertEqual(self.store.upsert_nodes([NodeSighting(KEY_B, "Twin", 150)], NOW), 0)
        # A flood advert decides; a later one from the other node doesn't move it.
        self.assertEqual(self.store.upsert_nodes([NodeSighting(KEY_C, "Twin", 200, flood=True)], NOW), 1)
        self.assertEqual(self.people()["Twin"].node_id, KEY_C)
        self.store.upsert_nodes([NodeSighting(KEY_B, "Twin", 300, flood=True)], NOW)
        self.assertEqual(self.people()["Twin"].node_id, KEY_C)
        self.assertEqual([h.participant_id for h in self.store.histories(WEEKLY)], [pid])

    def test_stale_flood_advert_doesnt_link_an_old_name(self):
        self.store.upsert_nodes([NodeSighting(KEY_A, "Current", 200)], NOW)
        self.store.record_check_in(record("Unlinked", "k1"))
        # A replayed older advert of KEY_A under a name it no longer has.
        self.store.upsert_nodes([NodeSighting(KEY_A, "Unlinked", 100, flood=True)], NOW)
        self.assertIsNone(self.people()["Unlinked"].node_id)

    def test_renamed_node_merges_its_name_only_history(self):
        self.store.upsert_nodes([NodeSighting(KEY_A, "Old", 100)], NOW)
        old = self.store.record_check_in(record("Old", "k1", day=MON - timedelta(weeks=1)))
        # It renames itself and checks in before its new advert is heard...
        self.store.record_check_in(record("New", "k2"))
        self.assertEqual(len(self.store.histories(WEEKLY)), 2)
        # ...then the advert arrives: one participant, both weeks, the new name.
        self.store.upsert_nodes([NodeSighting(KEY_A, "New", 200)], NOW)
        histories = self.store.histories(WEEKLY)
        self.assertEqual(len(histories), 1)
        self.assertEqual((histories[0].participant_id, histories[0].name, len(histories[0].periods)), (old, "New", 2))

    def test_older_or_guessed_names_dont_overwrite(self):
        self.store.upsert_nodes([NodeSighting(KEY_A, "Current", 200)], NOW)
        self.store.upsert_nodes([NodeSighting(KEY_A, "Stale", 100), NodeSighting(KEY_A, "Guess", fill_only=True)], NOW)
        self.assertEqual(self.store.search_nodes("cur")[0]["name"], "Current")
        self.assertEqual(self.store.search_nodes(KEY_A[:6].lower())[0]["node_id"], KEY_A)
        self.assertEqual(self.store.search_nodes("100%"), [])


class ManagementTests(StoreTestCase):
    def test_periods_and_streaks(self):
        pid = self.store.create_participant("Alpha", KEY_A, NOW)
        self.store.set_streak(pid, 3, MON, WEEKLY, place)
        self.assertEqual(set(self.people()["Alpha"].periods), {MON - timedelta(weeks=n) for n in range(3)})
        self.store.set_period(pid, MON - timedelta(weeks=5), True, WEEKLY, place)
        self.store.set_period(pid, MON - timedelta(weeks=3), True, WEEKLY, place)  # joins the run...
        self.store.set_streak(pid, 2, MON, WEEKLY, place)  # ...until the streak is set to 2
        periods = set(self.people()["Alpha"].periods)
        self.assertNotIn(MON - timedelta(weeks=2), periods)
        self.assertIn(MON - timedelta(weeks=5), periods)  # older history stays
        self.store.set_period(pid, MON, False, WEEKLY, place)
        self.assertNotIn(MON, self.people()["Alpha"].periods)

    def test_daily_periods_and_streaks(self):
        pid = self.store.create_participant("Alpha", KEY_A, NOW)
        self.store.set_streak(pid, 3, MON, DAILY, place)
        self.assertEqual(set(self.people(DAILY)["Alpha"].periods), {MON - timedelta(days=n) for n in range(3)})
        # Three days of one week are one weekly check-in.
        self.assertEqual(set(self.people(WEEKLY)["Alpha"].periods), {MON - timedelta(weeks=1), MON})
        self.store.set_period(pid, MON - timedelta(days=1), False, DAILY, place)
        self.assertEqual(set(self.people(DAILY)["Alpha"].periods), {MON, MON - timedelta(days=2)})

    def test_a_week_holds_the_check_ins_of_every_day_in_it(self):
        pid = self.store.record_check_in(record("Alpha", "k1", day=MON + timedelta(days=2)))
        self.store.record_check_in(record("Alpha", "k2", day=MON + timedelta(days=4), at=NOW + 2 * 86400))
        self.assertEqual(self.people(WEEKLY)["Alpha"].periods, {MON: NOW})
        self.assertEqual(len(self.people(DAILY)["Alpha"].periods), 2)
        # Adding the week changes nothing; removing it removes both days.
        self.store.set_period(pid, MON, True, WEEKLY, place)
        self.assertEqual(len(self.store.check_ins(pid)), 2)
        self.store.set_period(pid, MON, False, WEEKLY, place)
        self.assertEqual(self.store.check_ins(pid), [])

    def test_a_manual_weekly_check_in_goes_on_the_check_in_day(self):
        pid = self.store.create_participant("Alpha", KEY_A, NOW)
        friday = Schedule("weekly", 4)
        self.store.set_period(pid, MON, True, friday, place)
        self.assertEqual(self.store.check_ins(pid)[0]["day"], "2026-10-16")

    def test_create_update_link_and_delete(self):
        pid = self.store.create_participant("Alpha", KEY_A, NOW)
        with self.assertRaises(CheckInError) as caught:
            self.store.create_participant("Other", KEY_A, NOW)
        self.assertEqual(caught.exception.status, 409)
        other = self.store.record_check_in(record("Alias", "k1"))
        # Linking the name-only participant to Alpha's key merges it into Alpha.
        self.assertEqual(self.store.update_participant(other, now=NOW, node_id=KEY_A), pid)
        self.assertEqual([h.name for h in self.store.histories(WEEKLY)], ["Alpha"])
        self.assertEqual(self.store.update_participant(pid, now=NOW, name="Renamed", node_id=None), pid)
        self.assertEqual(self.people()["Renamed"].node_id, None)
        self.store.delete_participant(pid)
        self.assertEqual(self.store.histories(WEEKLY), [])
        with self.assertRaises(CheckInError) as caught:
            self.store.delete_participant(pid)
        self.assertEqual(caught.exception.status, 404)


class ImportTests(StoreTestCase):
    def test_import_by_key_prefix_and_name(self):
        self.store.upsert_nodes([NodeSighting(KEY_A, "Alpha", 100), NodeSighting(KEY_B, "Bravo", 100)], NOW)
        self.store.record_check_in(record("Charlie", "k1"))  # name only so far
        records = [
            SummaryRecord(1, KEY_A, None, 15, total=20, last_period=MON - timedelta(weeks=1)),
            SummaryRecord(2, "b2b2", None, 2, last_period=MON),
            SummaryRecord(3, None, "Charlie", 4, last_period=MON),
            SummaryRecord(4, None, "Delta", 0, total=3),
        ]
        result = self.store.import_summary(records, replace=False, dry_run=False, schedule=WEEKLY, place=place, now=NOW)
        self.assertEqual(result, {"created": 3, "updated": 1, "linked": 0, "errors": [], "saved": True})
        people = self.people()
        self.assertEqual((people["Alpha"].node_id, len(people["Alpha"].periods), people["Alpha"].extra_check_ins), (KEY_A, 15, 5))
        self.assertEqual(people["Bravo"].node_id, KEY_B)
        self.assertEqual(len(people["Charlie"].periods), 4)
        self.assertEqual(people["Delta"].extra_check_ins, 3)
        self.assertTrue(all(row["source"] == SOURCE_IMPORT for row in self.store.check_ins(people["Alpha"].participant_id)))

    def test_bad_records_or_dry_runs_change_nothing(self):
        records = [SummaryRecord(1, None, "Alpha", 3, last_period=MON), SummaryRecord(2, "ffff", None, 1, last_period=MON)]
        result = self.store.import_summary(records, replace=False, dry_run=False, schedule=WEEKLY, place=place, now=NOW)
        self.assertFalse(result["saved"])
        self.assertEqual(result["errors"], [{"row": 2, "message": "no known node has a key starting with FFFF"}])
        self.assertEqual(self.store.histories(WEEKLY), [])
        result = self.store.import_summary(records[:1], replace=False, dry_run=True, schedule=WEEKLY, place=place, now=NOW)
        self.assertEqual((result["created"], result["saved"]), (1, False))
        self.assertEqual(self.store.histories(WEEKLY), [])

    def test_replace(self):
        self.store.record_check_in(record("Old", "k1"))
        records = [SummaryRecord(1, KEY_A, "Alpha", 1, last_period=MON)]
        self.store.import_summary(records, replace=True, dry_run=False, schedule=WEEKLY, place=place, now=NOW)
        self.assertEqual([h.name for h in self.store.histories(WEEKLY)], ["Alpha"])


def as_backup(exported):
    """``export_backup`` rows -> what ``restore_backup`` takes (the plugin parses the file into these)."""
    return [
        BackupParticipant(
            row,
            item["node_id"],
            item["name"],
            item["extra_check_ins"],
            tuple(BackupCheckIn(**{**check_in, "day": date.fromisoformat(check_in["day"])}) for check_in in item["check_ins"]),
        )
        for row, item in enumerate(exported, start=1)
    ]


class BackupTests(StoreTestCase):
    def fill(self):
        self.store.upsert_nodes([NodeSighting(KEY_A, "Alpha", 100)], NOW)
        self.store.record_check_in(record("Alpha", "k1", day=MON - timedelta(weeks=1), at=NOW - 7 * 86400))
        self.store.record_check_in(record("Alpha", "k2"))
        self.store.record_check_in(record("Charlie", "k3", at=NOW + 60))  # name only
        manual = self.store.create_participant("Delta", None, NOW)
        self.store.set_streak(manual, 2, MON, WEEKLY, place)
        self.store.import_summary([SummaryRecord(1, None, "Echo", 0, total=4)], replace=False, dry_run=False, schedule=WEEKLY, place=place, now=NOW)

    def snapshot(self):
        return sorted(
            (h.name, h.node_id, h.extra_check_ins, tuple(sorted(h.periods.items())), h.last_check_in) for h in self.store.histories(WEEKLY)
        )

    def test_replace_brings_back_exactly_what_was_exported(self):
        self.fill()
        before, exported = self.snapshot(), self.store.export_backup()
        self.assertEqual(exported[0]["check_ins"][0]["message_key"], "k1")
        self.assertEqual(self.store.clear(), {"participants": 4, "check_ins": 5})
        self.assertEqual(self.store.histories(WEEKLY), [])
        self.store.record_check_in(record("Stranger", "k9"))
        result = self.store.restore_backup(as_backup(exported), replace=True, dry_run=False, now=NOW)
        self.assertEqual(result, {"created": 4, "updated": 0, "check_ins": 5, "duplicates": 0, "saved": True})
        self.assertEqual(self.snapshot(), before)
        # Every check-in comes back as it was, sources and texts included.
        self.assertEqual(self.store.export_backup(), exported)

    def test_merge_adds_what_isnt_here_yet(self):
        self.fill()
        exported = self.store.export_backup()
        before = self.snapshot()
        # Restoring into the same data changes nothing.
        result = self.store.restore_backup(as_backup(exported), replace=False, dry_run=False, now=NOW)
        self.assertEqual((result["created"], result["updated"], result["check_ins"], result["duplicates"]), (0, 4, 0, 5))
        self.assertEqual(self.snapshot(), before)
        # Into a fresh database with a check-in of its own: both are kept.
        self.store.clear()
        alpha = self.store.record_check_in(record("Alpha", "k5", day=MON + timedelta(weeks=1), at=NOW + 7 * 86400))
        result = self.store.restore_backup(as_backup(exported), replace=False, dry_run=False, now=NOW)
        self.assertEqual((result["created"], result["updated"], result["check_ins"]), (3, 1, 5))
        self.assertEqual(len(self.people()["Alpha"].periods), 3)
        self.assertEqual(self.people()["Alpha"].participant_id, alpha)

    def test_dry_run_changes_nothing(self):
        self.fill()
        exported = self.store.export_backup()
        self.store.clear()
        result = self.store.restore_backup(as_backup(exported), replace=False, dry_run=True, now=NOW)
        self.assertEqual((result["created"], result["check_ins"], result["saved"]), (4, 5, False))
        self.assertEqual(self.store.histories(WEEKLY), [])

    def test_restored_node_goes_by_its_advertised_name(self):
        self.store.upsert_nodes([NodeSighting(KEY_B, "Renamed", 100)], NOW)
        backup = [BackupParticipant(1, KEY_B, "Old name", 0, (BackupCheckIn(MON, NOW, SOURCE_MANUAL),))]
        self.store.restore_backup(backup, replace=False, dry_run=False, now=NOW)
        self.assertEqual(self.people()["Renamed"].node_id, KEY_B)


class FileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "check_in.sqlite3"

    def test_data_survives_reopening(self):
        store = CheckInStore(self.path)
        store.open()
        store.record_check_in(record("Alpha", "k1"))
        store.close()
        store = CheckInStore(self.path)
        store.open()
        self.addCleanup(store.close)
        self.assertEqual([h.name for h in store.histories(WEEKLY)], ["Alpha"])

    def test_schema_1_check_ins_get_their_day(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE participants (id INTEGER PRIMARY KEY, node_id TEXT UNIQUE, name TEXT NOT NULL,
                extra_check_ins INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE check_ins (id INTEGER PRIMARY KEY,
                participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
                week TEXT NOT NULL, received_at REAL NOT NULL, sender_name TEXT, text TEXT, source TEXT NOT NULL,
                observer_id TEXT, message_key TEXT UNIQUE);
            CREATE INDEX check_ins_by_participant ON check_ins (participant_id, week);
            CREATE TABLE nodes (node_id TEXT PRIMARY KEY, name TEXT NOT NULL, advert_at REAL, seen_at REAL NOT NULL);
            PRAGMA user_version=1;
            """
        )
        wednesday_night = datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc).timestamp()
        conn.execute("INSERT INTO participants VALUES (1, NULL, 'Alpha', 0, 0, 0)")
        conn.executemany(
            "INSERT INTO check_ins (participant_id, week, received_at, source, message_key) VALUES (1, ?, ?, ?, ?)",
            [
                # Late on Wednesday in UTC is Thursday in Rome.
                ("2026-10-12", wednesday_night, SOURCE_RADIO, "k1"),
                # Placed in another week than it was received in: its week's Monday.
                ("2026-10-05", wednesday_night, SOURCE_MANUAL, None),
            ],
        )
        conn.commit()
        conn.close()
        store = CheckInStore(self.path, zoneinfo.ZoneInfo("Europe/Rome"))
        store.open()
        self.addCleanup(store.close)
        self.assertEqual(sorted(row["day"] for row in store.check_ins(1)), ["2026-10-05", "2026-10-15"])
        self.assertEqual(set(store.histories(WEEKLY)[0].periods), {date(2026, 10, 5), MON})
        # Keys are still unique, and new check-ins go in.
        self.assertIsNone(store.record_check_in(record("Alpha", "k1")))
        self.assertIsNotNone(store.record_check_in(record("Alpha", "k2")))
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        conn.close()

    def test_newer_schema_is_refused(self):
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(CheckInStoreError):
            CheckInStore(self.path).open()

    def test_corrupt_file_is_moved_aside(self):
        self.path.write_bytes(b"this is not a database" * 100)
        store = CheckInStore(self.path)
        with self.assertLogs("meshgram.plugins.check_in_store", "ERROR"):
            store.open()
        self.addCleanup(store.close)
        self.assertEqual(store.histories(WEEKLY), [])
        self.assertTrue(any(p.name.startswith("check_in.sqlite3.corrupt-") for p in self.path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()

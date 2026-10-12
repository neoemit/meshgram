import unittest
import zoneinfo
from datetime import date, datetime, timedelta, timezone

from meshgram.plugins.check_in_scoring import (
    History,
    best_streak,
    check_in_window,
    current_streak,
    is_check_in_day,
    local_moment,
    midnight,
    parse_week,
    standings,
    week_start,
)

ROME = zoneinfo.ZoneInfo("Europe/Rome")
MONDAY = frozenset({0})
MON = date(2026, 10, 12)  # a Monday


def weeks_back(count: int, last: date = MON) -> set[date]:
    return {last - timedelta(weeks=offset) for offset in range(count)}


class WeekTests(unittest.TestCase):
    def test_weeks_start_on_monday(self):
        self.assertEqual(week_start(date(2026, 10, 18)), MON)  # Sunday
        self.assertEqual(week_start(MON), MON)
        self.assertEqual(parse_week("2026-10-14"), MON)
        with self.assertRaises(ValueError):
            parse_week("not a date")

    def test_days_and_weeks_follow_the_time_zone(self):
        # 23:30 UTC on Sunday is already Monday in Rome.
        sunday_night = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc).timestamp()
        self.assertFalse(is_check_in_day(local_moment(sunday_night, timezone.utc), MONDAY))
        moment = local_moment(sunday_night, ROME)
        self.assertTrue(is_check_in_day(moment, MONDAY))
        self.assertEqual(week_start(moment.date()), MON)
        self.assertTrue(is_check_in_day(moment, frozenset()))  # no days configured: any day

    def test_window_is_the_check_in_day(self):
        monday_morning = datetime(2026, 10, 12, 9, 0, tzinfo=ROME)
        window = check_in_window(monday_morning, MONDAY)
        self.assertTrue(window.open)
        self.assertEqual((window.opens_at, window.closes_at), (midnight(MON, ROME), midnight(date(2026, 10, 13), ROME)))

        wednesday = datetime(2026, 10, 14, 9, 0, tzinfo=ROME)
        window = check_in_window(wednesday, MONDAY)
        self.assertFalse(window.open)
        self.assertEqual(window.opens_at, midnight(date(2026, 10, 19), ROME))

    def test_consecutive_days_are_one_window(self):
        saturday = datetime(2026, 10, 17, 9, 0, tzinfo=ROME)
        window = check_in_window(saturday, frozenset({5, 6}))
        self.assertEqual((window.open, window.closes_at), (True, midnight(date(2026, 10, 19), ROME)))
        # Sunday and Monday, across the start of a week.
        sunday = datetime(2026, 10, 18, 9, 0, tzinfo=ROME)
        window = check_in_window(sunday, frozenset({6, 0}))
        self.assertEqual((window.opens_at.date(), window.closes_at.date()), (date(2026, 10, 18), date(2026, 10, 20)))
        # Any day: the whole week.
        window = check_in_window(saturday, frozenset())
        self.assertEqual((window.open, window.opens_at.date(), window.closes_at.date()), (True, MON, date(2026, 10, 19)))

    def test_window_across_daylight_saving_time(self):
        # Clocks go forward in Rome on Sunday 29 March 2026: that day has 23 hours.
        window = check_in_window(datetime(2026, 3, 29, 12, 0, tzinfo=ROME), frozenset({6}))
        self.assertEqual(window.closes_at.timestamp() - window.opens_at.timestamp(), 23 * 3600)
        self.assertEqual(window.closes_at.utcoffset(), timedelta(hours=2))


class StreakTests(unittest.TestCase):
    def test_current_streak(self):
        self.assertEqual(current_streak(weeks_back(3), MON, still_open=False), 3)
        last_week = MON - timedelta(weeks=1)
        # Not checked in yet this week: last week's streak holds while the window is still to come...
        self.assertEqual(current_streak(weeks_back(4, last_week), MON, still_open=True), 4)
        # ...and is broken once it has passed.
        self.assertEqual(current_streak(weeks_back(4, last_week), MON, still_open=False), 0)
        # A gap ends it.
        self.assertEqual(current_streak({MON, MON - timedelta(weeks=2)}, MON, still_open=False), 1)
        self.assertEqual(current_streak(set(), MON, still_open=True), 0)

    def test_best_streak(self):
        self.assertEqual(best_streak([]), 0)
        weeks = weeks_back(2) | weeks_back(5, MON - timedelta(weeks=4))
        self.assertEqual(best_streak(weeks), 5)


class StandingsTests(unittest.TestCase):
    def test_order_ranks_and_this_weeks_order(self):
        monday_noon = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        first = monday_noon.timestamp() - 3600
        histories = [
            History(1, "A" * 64, "Alpha", {week: first for week in weeks_back(2)}, first),
            History(2, None, "Bravo", {week: first + 60 for week in weeks_back(5)}, first + 60),
            History(3, "C" * 64, "Charlie", {week: first - 60 for week in weeks_back(2)}, first - 60, extra_check_ins=4),
            History(4, "D" * 64, "Delta", {MON - timedelta(weeks=3): first}, first),
        ]
        table = standings(histories, monday_noon, MONDAY)
        self.assertEqual([s.name for s in table], ["Bravo", "Charlie", "Alpha", "Delta"])
        self.assertEqual([s.rank for s in table], [1, 2, 2, 4])  # equal streaks share a rank
        self.assertEqual([s.streak for s in table], [5, 2, 2, 0])
        self.assertEqual(table[1].total_check_ins, 6)
        # This week's order: Charlie first, then Alpha, then Bravo.
        self.assertEqual({s.name: s.week_rank for s in table}, {"Charlie": 1, "Alpha": 2, "Bravo": 3, "Delta": None})
        self.assertFalse(table[3].checked_in_this_week)


if __name__ == "__main__":
    unittest.main()

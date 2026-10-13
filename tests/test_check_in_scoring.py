import unittest
import zoneinfo
from datetime import date, datetime, timedelta, timezone

from meshgram.plugins.check_in_scoring import (
    DAY,
    History,
    Schedule,
    best_streak,
    current_streak,
    local_moment,
    midnight,
    parse_date,
    standings,
    week_start,
)

ROME = zoneinfo.ZoneInfo("Europe/Rome")
MONDAY = Schedule("weekly", 0)
DAILY = Schedule("daily")
MON = date(2026, 10, 12)  # a Monday


def weeks_back(count: int, last: date = MON) -> set[date]:
    return {last - timedelta(weeks=offset) for offset in range(count)}


def days_back(count: int, last: date = MON) -> set[date]:
    return {last - timedelta(days=offset) for offset in range(count)}


class ScheduleTests(unittest.TestCase):
    def test_weeks_start_on_monday(self):
        self.assertEqual(week_start(date(2026, 10, 18)), MON)  # Sunday
        self.assertEqual(week_start(MON), MON)
        self.assertEqual(parse_date("2026-10-14"), date(2026, 10, 14))
        with self.assertRaises(ValueError):
            parse_date("not a date")

    def test_periods(self):
        wednesday = date(2026, 10, 14)
        self.assertEqual((MONDAY.period_of(wednesday), MONDAY.step, MONDAY.unit), (MON, timedelta(days=7), "week"))
        self.assertEqual((DAILY.period_of(wednesday), DAILY.step, DAILY.unit), (wednesday, DAY, "day"))
        self.assertEqual(Schedule("weekly", 2).check_in_day(MON), wednesday)
        self.assertEqual(DAILY.check_in_day(wednesday), wednesday)

    def test_invalid_schedules(self):
        with self.assertRaises(ValueError):
            Schedule("monthly")
        with self.assertRaises(ValueError):
            Schedule("weekly", 7)

    def test_days_and_weeks_follow_the_time_zone(self):
        # 23:30 UTC on Sunday is already Monday in Rome.
        sunday_night = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc).timestamp()
        self.assertFalse(MONDAY.counts_on(local_moment(sunday_night, timezone.utc).date()))
        moment = local_moment(sunday_night, ROME)
        self.assertTrue(MONDAY.counts_on(moment.date()))
        self.assertEqual(MONDAY.period_of(moment.date()), MON)
        # Daily: any day counts.
        self.assertTrue(all(DAILY.counts_on(MON + timedelta(days=n)) for n in range(7)))

    def test_weekly_window_is_the_check_in_day(self):
        monday_morning = datetime(2026, 10, 12, 9, 0, tzinfo=ROME)
        window = MONDAY.window(monday_morning)
        self.assertTrue(window.open)
        self.assertEqual((window.opens_at, window.closes_at), (midnight(MON, ROME), midnight(date(2026, 10, 13), ROME)))

        wednesday = datetime(2026, 10, 14, 9, 0, tzinfo=ROME)
        window = MONDAY.window(wednesday)
        self.assertFalse(window.open)
        self.assertEqual(window.opens_at, midnight(date(2026, 10, 19), ROME))
        # A Friday check-in day, seen on Wednesday: this week's.
        window = Schedule("weekly", 4).window(wednesday)
        self.assertEqual((window.open, window.opens_at.date()), (False, date(2026, 10, 16)))

    def test_daily_window_is_today(self):
        wednesday = datetime(2026, 10, 14, 9, 0, tzinfo=ROME)
        window = DAILY.window(wednesday)
        self.assertEqual(
            (window.open, window.opens_at, window.closes_at),
            (True, midnight(date(2026, 10, 14), ROME), midnight(date(2026, 10, 15), ROME)),
        )

    def test_window_across_daylight_saving_time(self):
        # Clocks go forward in Rome on Sunday 29 March 2026: that day has 23 hours.
        window = Schedule("weekly", 6).window(datetime(2026, 3, 29, 12, 0, tzinfo=ROME))
        self.assertEqual(window.closes_at.timestamp() - window.opens_at.timestamp(), 23 * 3600)
        self.assertEqual(window.closes_at.utcoffset(), timedelta(hours=2))

    def test_still_open_and_latest_period(self):
        sunday = datetime(2026, 10, 18, 9, 0, tzinfo=ROME)
        monday = datetime(2026, 10, 12, 9, 0, tzinfo=ROME)
        friday = Schedule("weekly", 4)
        self.assertTrue(friday.still_open(monday))
        self.assertFalse(friday.still_open(sunday))
        self.assertTrue(DAILY.still_open(sunday))
        # This week's Friday hasn't come yet on Monday: the latest check-in week is last week.
        self.assertEqual(friday.latest_period(monday), MON - timedelta(weeks=1))
        self.assertEqual(friday.latest_period(sunday), MON)
        self.assertEqual(DAILY.latest_period(sunday), date(2026, 10, 18))


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

    def test_daily_streak(self):
        self.assertEqual(current_streak(days_back(5), MON, still_open=True, step=DAY), 5)
        # Yesterday's streak holds today.
        self.assertEqual(current_streak(days_back(5, MON - DAY), MON, still_open=True, step=DAY), 5)
        # Weekly check-ins (a week apart) aren't a daily streak.
        self.assertEqual(current_streak(weeks_back(3), MON, still_open=True, step=DAY), 1)

    def test_best_streak(self):
        self.assertEqual(best_streak([]), 0)
        weeks = weeks_back(2) | weeks_back(5, MON - timedelta(weeks=4))
        self.assertEqual(best_streak(weeks), 5)
        self.assertEqual(best_streak(days_back(3) | days_back(4, MON - timedelta(days=10)), DAY), 4)


class StandingsTests(unittest.TestCase):
    def test_order_ranks_and_this_weeks_order(self):
        monday_noon = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        first = monday_noon.timestamp() - 3600
        histories = [
            History(1, "A" * 64, "Alpha", {week: first for week in weeks_back(2)}, first, extra_check_ins=4),
            History(2, None, "Bravo", {week: first + 60 for week in weeks_back(5)}, first + 60),
            History(3, "C" * 64, "Charlie", {week: first - 60 for week in weeks_back(2)}, first - 60),
            History(4, "D" * 64, "Delta", {MON - timedelta(weeks=3): first}, first),
        ]
        table = standings(histories, monday_noon, MONDAY)
        # Equal streaks: Charlie checked in before Alpha, more check-ins or not.
        self.assertEqual([s.name for s in table], ["Bravo", "Charlie", "Alpha", "Delta"])
        self.assertEqual([s.rank for s in table], [1, 2, 3, 4])
        self.assertEqual([s.streak for s in table], [5, 2, 2, 0])
        self.assertEqual(table[2].total_check_ins, 6)
        # This week's order: Charlie first, then Alpha, then Bravo.
        self.assertEqual({s.name: s.period_rank for s in table}, {"Charlie": 1, "Alpha": 2, "Bravo": 3, "Delta": None})
        self.assertFalse(table[3].checked_in_this_period)

    def test_equal_streaks_checked_in_this_week_first_and_no_streak_shares_a_rank(self):
        monday_noon = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        now = monday_noon.timestamp()
        last_week = MON - timedelta(weeks=1)
        histories = [
            # Still on last week's streak of 2 (checked in early that Monday), not yet this week.
            History(1, "A" * 64, "Alpha", {week: now - 7 * 86400 - 3600 for week in weeks_back(2, last_week)}, now - 7 * 86400),
            # A streak of 2 up to this week, checked in a minute ago.
            History(2, "B" * 64, "Bravo", {week: now - 60 for week in weeks_back(2)}, now - 60),
            History(3, "C" * 64, "Charlie", {MON - timedelta(weeks=4): now}, now),
            History(4, "D" * 64, "Delta", {MON - timedelta(weeks=6): now}, now),
        ]
        table = standings(histories, monday_noon, MONDAY)
        self.assertEqual([s.name for s in table], ["Bravo", "Alpha", "Charlie", "Delta"])
        self.assertEqual([s.streak for s in table], [2, 2, 0, 0])
        self.assertEqual([s.rank for s in table], [1, 2, 3, 3])

    def test_daily_standings(self):
        noon = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        now = noon.timestamp()
        histories = [
            # Every day for a week, not yet today: yesterday's streak of 7 holds.
            History(1, "A" * 64, "Alpha", {day: now - 86400 for day in days_back(7, MON - DAY)}, now - 86400),
            # Today and yesterday.
            History(2, "B" * 64, "Bravo", {day: now - 60 for day in days_back(2)}, now - 60),
            # Two days ago: broken.
            History(3, "C" * 64, "Charlie", {MON - 2 * DAY: now}, now),
        ]
        table = standings(histories, noon, DAILY)
        self.assertEqual([(s.name, s.streak) for s in table], [("Alpha", 7), ("Bravo", 2), ("Charlie", 0)])
        self.assertEqual([s.period_rank for s in table], [None, 1, None])


if __name__ == "__main__":
    unittest.main()

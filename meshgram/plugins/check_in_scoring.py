"""Week, window, streak and ranking arithmetic for the check_in plugin (no I/O).

Weeks run Monday to Sunday in the plugin's time zone and are identified by the
date of their Monday. A check-in counts on the configured weekdays only (all of
them when none are configured); a streak is the number of consecutive weeks with
at least one check-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Iterable, Optional

WEEK = timedelta(days=7)
DAY = timedelta(days=1)
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=day.weekday())


def local_moment(timestamp: float, zone: tzinfo) -> datetime:
    return datetime.fromtimestamp(timestamp, zone)


def midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone)


def is_check_in_day(moment: datetime, days: frozenset[int]) -> bool:
    return not days or moment.weekday() in days


def week_still_open(moment: datetime, days: frozenset[int]) -> bool:
    """Whether a check-in day of ``moment``'s week is under way or still to come."""
    return not days or any(day >= moment.weekday() for day in days)


@dataclass(frozen=True, slots=True)
class Window:
    """The check-in window under way (``open``) or the next one, in local time."""

    open: bool
    opens_at: datetime
    closes_at: datetime


def check_in_window(moment: datetime, days: frozenset[int]) -> Window:
    """Consecutive check-in days form one window (Saturday and Sunday, say)."""
    zone = moment.tzinfo
    assert zone is not None, "moment must be timezone-aware"
    today = moment.date()
    if not days or len(days) == 7:
        monday = week_start(today)
        return Window(True, midnight(monday, zone), midnight(monday + WEEK, zone))
    if today.weekday() in days:
        start, is_open = today, True
        while (start - DAY).weekday() in days:
            start -= DAY
    else:
        start, is_open = today + DAY, False
        while start.weekday() not in days:
            start += DAY
    end = start + DAY
    while end.weekday() in days:
        end += DAY
    return Window(is_open, midnight(start, zone), midnight(end, zone))


def current_streak(weeks: Iterable[date], this_week: date, still_open: bool) -> int:
    """Consecutive weeks with a check-in, up to this week.

    A participant who hasn't checked in yet this week keeps last week's streak while
    ``still_open`` (a check-in day is under way or to come); after that it's broken.
    """
    weeks = set(weeks)
    if this_week in weeks:
        week = this_week
    elif still_open and this_week - WEEK in weeks:
        week = this_week - WEEK
    else:
        return 0
    streak = 0
    while week in weeks:
        streak += 1
        week -= WEEK
    return streak


def best_streak(weeks: Iterable[date]) -> int:
    best = run = 0
    previous: Optional[date] = None
    for week in sorted(set(weeks)):
        run = run + 1 if previous is not None and week - previous == WEEK else 1
        best = max(best, run)
        previous = week
    return best


@dataclass(slots=True)
class History:
    """A participant's check-ins: the first one of each week (Unix time) and the latest."""

    participant_id: int
    node_id: Optional[str]
    name: str
    weeks: dict[date, float] = field(default_factory=dict)
    last_check_in: Optional[float] = None
    # Check-ins imported as a total, beyond the weeks on record.
    extra_check_ins: int = 0


@dataclass(slots=True)
class Standing:
    participant_id: int
    node_id: Optional[str]
    name: str
    streak: int
    best_streak: int
    total_check_ins: int
    last_check_in: Optional[float]
    checked_in_this_week: bool
    # Order of this week's first check-ins (1 = first); None when not checked in this week.
    week_rank: Optional[int]
    # Competition rank by streak (equal streaks share a rank).
    rank: int = 0


def standings(histories: Iterable[History], moment: datetime, days: frozenset[int]) -> list[Standing]:
    """Everyone, best first: by streak, then total check-ins, best streak and name."""
    histories = list(histories)
    this_week = week_start(moment.date())
    still_open = week_still_open(moment, days)
    this_week_order = sorted(
        (history for history in histories if this_week in history.weeks),
        key=lambda history: (history.weeks[this_week], history.participant_id),
    )
    week_ranks = {history.participant_id: index for index, history in enumerate(this_week_order, start=1)}

    result = [
        Standing(
            participant_id=history.participant_id,
            node_id=history.node_id,
            name=history.name,
            streak=current_streak(history.weeks, this_week, still_open),
            best_streak=best_streak(history.weeks),
            total_check_ins=len(history.weeks) + max(0, history.extra_check_ins),
            last_check_in=history.last_check_in,
            checked_in_this_week=history.participant_id in week_ranks,
            week_rank=week_ranks.get(history.participant_id),
        )
        for history in histories
    ]
    result.sort(key=lambda s: (-s.streak, -s.total_check_ins, -s.best_streak, s.name.casefold(), s.participant_id))
    previous_streak: Optional[int] = None
    for index, standing in enumerate(result, start=1):
        standing.rank = result[index - 2].rank if standing.streak == previous_streak else index
        previous_streak = standing.streak
    return result


def parse_week(value: str) -> date:
    """``YYYY-MM-DD`` (any day of the week) -> that week's Monday. Raises ``ValueError``."""
    return week_start(date.fromisoformat(str(value).strip()))

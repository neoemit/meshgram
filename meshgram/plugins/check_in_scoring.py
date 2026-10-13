"""Period, window, streak and ranking arithmetic for the check_in plugin (no I/O).

Check-ins are counted per *period* of a ``Schedule``: a day (daily check-ins) or a
week, Monday to Sunday (weekly check-ins), in the plugin's time zone. A period is
identified by the date it starts on: the day itself, or that week's Monday.
Daily check-ins count on any day; weekly ones on the schedule's one weekday only.
A streak is the number of consecutive periods with at least one check-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Iterable, Optional

WEEK = timedelta(days=7)
DAY = timedelta(days=1)
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

WEEKLY = "weekly"
DAILY = "daily"
FREQUENCIES = (WEEKLY, DAILY)


def week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=day.weekday())


def local_moment(timestamp: float, zone: tzinfo) -> datetime:
    return datetime.fromtimestamp(timestamp, zone)


def midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone)


def parse_date(value: str) -> date:
    """``YYYY-MM-DD`` -> that date. Raises ``ValueError``."""
    return date.fromisoformat(str(value).strip())


@dataclass(frozen=True, slots=True)
class Window:
    """The check-in window under way (``open``) or the next one, in local time."""

    open: bool
    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True, slots=True)
class Schedule:
    """When check-ins count: every day (``daily``), or once a week on ``day`` (0 = Monday)."""

    frequency: str = WEEKLY
    day: int = 0

    def __post_init__(self) -> None:
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {', '.join(FREQUENCIES)}")
        if not 0 <= self.day <= 6:
            raise ValueError("day must be 0 (Monday) to 6 (Sunday)")

    @property
    def daily(self) -> bool:
        return self.frequency == DAILY

    @property
    def step(self) -> timedelta:
        """How long a period is."""
        return DAY if self.daily else WEEK

    @property
    def unit(self) -> str:
        """What a period is called: "day" or "week"."""
        return "day" if self.daily else "week"

    def period_of(self, day: date) -> date:
        """The period ``day`` is in: the day itself, or its week's Monday."""
        return day if self.daily else week_start(day)

    def check_in_day(self, period: date) -> date:
        """The date check-ins count on in ``period``."""
        return period if self.daily else period + timedelta(days=self.day)

    def counts_on(self, day: date) -> bool:
        """Whether a check-in on ``day`` counts."""
        return self.daily or day.weekday() == self.day

    def still_open(self, moment: datetime) -> bool:
        """Whether the check-in day of ``moment``'s period is under way or still to come."""
        return self.daily or moment.weekday() <= self.day

    def latest_period(self, moment: datetime) -> date:
        """The latest period whose check-in day has begun: the current one, or (weekly) last week."""
        period = self.period_of(moment.date())
        return period if self.check_in_day(period) <= moment.date() else period - self.step

    def window(self, moment: datetime) -> Window:
        """The check-in day under way, or the next one."""
        zone = moment.tzinfo
        assert zone is not None, "moment must be timezone-aware"
        today = moment.date()
        if self.counts_on(today):
            return Window(True, midnight(today, zone), midnight(today + DAY, zone))
        start = today + timedelta(days=(self.day - today.weekday()) % 7)
        return Window(False, midnight(start, zone), midnight(start + DAY, zone))


def streak_end(periods: Iterable[date], this_period: date, still_open: bool, step: timedelta = WEEK) -> Optional[date]:
    """The current streak's latest period: this one, the one before while ``still_open``, or None (no streak)."""
    periods = set(periods)
    if this_period in periods:
        return this_period
    if still_open and this_period - step in periods:
        return this_period - step
    return None


def current_streak(periods: Iterable[date], this_period: date, still_open: bool, step: timedelta = WEEK) -> int:
    """Consecutive periods with a check-in, up to this one.

    A participant who hasn't checked in yet this period keeps the previous period's
    streak while ``still_open`` (a check-in day is under way or to come); after that
    it's broken.
    """
    periods = set(periods)
    period = streak_end(periods, this_period, still_open, step)
    if period is None:
        return 0
    streak = 0
    while period in periods:
        streak += 1
        period -= step
    return streak


def best_streak(periods: Iterable[date], step: timedelta = WEEK) -> int:
    best = run = 0
    previous: Optional[date] = None
    for period in sorted(set(periods)):
        run = run + 1 if previous is not None and period - previous == step else 1
        best = max(best, run)
        previous = period
    return best


@dataclass(slots=True)
class History:
    """A participant's check-ins: the first one of each period (Unix time) and the latest."""

    participant_id: int
    node_id: Optional[str]
    name: str
    periods: dict[date, float] = field(default_factory=dict)
    last_check_in: Optional[float] = None
    # Check-ins imported as a total, beyond the periods on record.
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
    checked_in_this_period: bool
    # Order of this period's first check-ins (1 = first); None when not checked in this period.
    period_rank: Optional[int]
    # Competition rank by streak, equal streaks by who checked in first (those without a streak share one).
    rank: int = 0


def standings(histories: Iterable[History], moment: datetime, schedule: Schedule) -> list[Standing]:
    """Everyone, best first: by streak, then who checked in first, total check-ins, best streak and name.

    Of equal streaks, those already checked in this period come before those still on
    the previous one's, each by the time of that period's first check-in, earliest first.
    """
    histories = list(histories)
    step = schedule.step
    this_period = schedule.period_of(moment.date())
    still_open = schedule.still_open(moment)
    this_period_order = sorted(
        (history for history in histories if this_period in history.periods),
        key=lambda history: (history.periods[this_period], history.participant_id),
    )
    period_ranks = {history.participant_id: index for index, history in enumerate(this_period_order, start=1)}

    ranked: list[tuple[tuple, Standing]] = []
    for history in histories:
        end = streak_end(history.periods, this_period, still_open, step)
        standing = Standing(
            participant_id=history.participant_id,
            node_id=history.node_id,
            name=history.name,
            streak=current_streak(history.periods, this_period, still_open, step),
            best_streak=best_streak(history.periods, step),
            total_check_ins=len(history.periods) + max(0, history.extra_check_ins),
            last_check_in=history.last_check_in,
            checked_in_this_period=history.participant_id in period_ranks,
            period_rank=period_ranks.get(history.participant_id),
        )
        ranked.append(((-standing.streak, (-end.toordinal(), history.periods[end]) if end else ()), standing))
    ranked.sort(key=lambda pair: (
        pair[0], -pair[1].total_check_ins, -pair[1].best_streak, pair[1].name.casefold(), pair[1].participant_id,
    ))
    result: list[Standing] = []
    for index, (key, standing) in enumerate(ranked):
        standing.rank = result[-1].rank if index and key == ranked[index - 1][0] else index + 1
        result.append(standing)
    return result

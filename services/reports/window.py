"""The daily brief's time window (ADR 0009).

Pure: no clock, no database. The caller supplies ``now`` and every boundary follows from it,
so a window is reproducible -- the same ``now`` always yields the same window, which is what
lets a regenerated brief cover exactly the ground the first one covered.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

#: ADR 0009 anchors the brief to the US market day, not to UTC: a UTC boundary would drift
#: an hour into the trading day twice a year, which is the one thing the schedule must not do.
BRIEF_TIMEZONE = ZoneInfo("America/New_York")

#: A brief covers the window ending at 05:30 ET, and 05:30 is a deliberate choice of hour as
#: well as of timezone: it exists exactly once on every US calendar date. The spring-forward
#: gap is 02:00-03:00 and the fall-back fold is 01:00-02:00, so unlike (say) 01:30 or 02:30,
#: 05:30 is never ambiguous and never missing, and the cutoff is always a single instant.
#:
#: What DST *does* change is the window's length: the window closing on the spring-forward
#: date is 23 hours long and the one closing on the fall-back date is 25. That is correct and
#: intended -- the window is "the last calendar day, ET", not "the last 24 hours".
BRIEF_CUTOFF_HOUR = 5
BRIEF_CUTOFF_MINUTE = 30

ONE_DAY = datetime.timedelta(days=1)


class NaiveDatetimeError(ValueError):
    """A naive datetime reached a window boundary.

    Every boundary here is an *instant*, and a naive datetime does not name one: comparing it
    against the cutoff would silently assume a timezone and put events in the wrong brief. The
    assumption is refused instead.
    """


def _require_aware(value: datetime.datetime, field: str) -> datetime.datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = f"{field} must be timezone-aware; got {value!r}"
        raise NaiveDatetimeError(msg)
    return value


def _instant(value: datetime.datetime) -> datetime.datetime:
    """Convert to UTC before any comparison or subtraction. Not optional -- see below.

    Python compares and subtracts two aware datetimes that share a ``tzinfo`` *object* by wall
    clock, ignoring their UTC offsets ("the common tzinfo attribute is ignored and the base
    datetimes are compared"). Both cutoffs of a window carry the same :data:`BRIEF_TIMEZONE`
    instance, so the naive reading of ``end - start`` returns 24h on every date -- including
    the spring-forward date, whose window is really 23h -- and a moment handed in on ET wall
    time would sort into the wrong side of a fall-back fold. Going through UTC makes every
    boundary an instant again, which is the only thing a window boundary can honestly be.
    """
    return value.astimezone(datetime.UTC)


def cutoff_for(brief_date: datetime.date) -> datetime.datetime:
    """The 05:30 ET instant that closes ``brief_date``'s window."""
    return datetime.datetime(
        brief_date.year,
        brief_date.month,
        brief_date.day,
        BRIEF_CUTOFF_HOUR,
        BRIEF_CUTOFF_MINUTE,
        tzinfo=BRIEF_TIMEZONE,
    )


@dataclass(frozen=True)
class BriefWindow:
    """The half-open interval ``(start, end]`` one daily brief covers.

    ``brief_date`` is the ET calendar date of ``end``, per ADR 0009 -- so the brief published
    on the morning of the 8th is the brief *for* the 8th, covering the 8th's cutoff back to
    the 7th's.
    """

    brief_date: datetime.date
    #: The previous cutoff. Exclusive: it belongs to the previous brief, which closed on it.
    start: datetime.datetime
    #: This brief's cutoff. Inclusive, so no instant falls between two consecutive briefs.
    end: datetime.datetime

    @property
    def duration(self) -> datetime.timedelta:
        """23h, 24h, or 25h -- see :data:`BRIEF_CUTOFF_HOUR` and :func:`_instant`."""
        return _instant(self.end) - _instant(self.start)

    def contains(self, moment: datetime.datetime) -> bool:
        """ADR 0009's qualifying predicate: ``moment`` in ``(start, end]``."""
        _require_aware(moment, "moment")
        return _instant(self.start) < _instant(moment) <= _instant(self.end)

    def is_developing(self, began_at: datetime.datetime | None) -> bool:
        """Did an event that updated into this window *begin* before this window?

        ADR 0009: an event straddling two windows appears in the next brief with a
        "developing" marker rather than being split. The comparison is ``<= start`` and not
        ``< start`` because the previous window is *closed* at its end: an event that began at
        exactly the previous cutoff began inside the previous window, and so is straddling by
        the time it updates into this one.

        ``None`` is not developing. A missing ``first_seen_at`` is not evidence that the event
        is new -- it is evidence of nothing, and the caller records it as a data-quality note
        rather than letting an absence mark a story as breaking.
        """
        if began_at is None:
            return False
        _require_aware(began_at, "began_at")
        return _instant(began_at) <= _instant(self.start)


def window_for_date(brief_date: datetime.date) -> BriefWindow:
    """The window of the brief for ``brief_date``."""
    return BriefWindow(
        brief_date=brief_date,
        start=cutoff_for(brief_date - ONE_DAY),
        end=cutoff_for(brief_date),
    )


def window_for(now: datetime.datetime) -> BriefWindow:
    """The window covered by a brief generated at ``now``.

    The cutoff is inclusive, so a run at exactly 05:30 ET closes today's window; a run one
    second earlier is still inside yesterday's.
    """
    _require_aware(now, "now")
    local = now.astimezone(BRIEF_TIMEZONE)
    brief_date = local.date()
    if (local.hour, local.minute) < (BRIEF_CUTOFF_HOUR, BRIEF_CUTOFF_MINUTE):
        brief_date -= ONE_DAY
    return window_for_date(brief_date)

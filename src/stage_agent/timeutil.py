"""Time helpers. Everything internal is timezone-aware UTC; IST (UTC+05:30) is used for quiet hours and day caps."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def ist_hour(dt: datetime) -> float:
    t = dt.astimezone(IST)
    return t.hour + t.minute / 60.0


def in_quiet_hours(dt: datetime, start_h: int = 22, end_h: int = 8) -> bool:
    h = dt.astimezone(IST).hour
    return h >= start_h or h < end_h


def next_send_window(dt: datetime, start_h: int = 8, end_h: int = 22) -> datetime:
    """Earliest instant >= dt that is outside quiet hours (IST)."""
    if not in_quiet_hours(dt, end_h, start_h):
        return dt
    local = dt.astimezone(IST)
    if local.hour >= end_h:
        local = (local + timedelta(days=1)).replace(hour=start_h, minute=0, second=0, microsecond=0)
    else:
        local = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


def at_ist_hour(day: date, hour: float) -> datetime:
    h = int(hour)
    m = int(round((hour - h) * 60))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST).astimezone(UTC)


def seconds(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds()

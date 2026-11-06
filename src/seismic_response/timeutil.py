"""Small helpers for ISO 8601 / evalresp time handling (UTC throughout)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

__all__ = ["parse_iso_time", "format_iso_time", "ensure_utc", "now_utc"]

_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?\s*$"
)


def parse_iso_time(value: str) -> datetime:
    """Parse an ISO 8601 date/time; naive values are taken to be UTC.

    >>> parse_iso_time("2014-12-17T18:40:00").isoformat()
    '2014-12-17T18:40:00+00:00'
    >>> parse_iso_time("2014-12-17T18:40:00.5Z").microsecond
    500000
    """
    m = _ISO_RE.match(value)
    if not m:
        raise ValueError(f"Cannot parse time {value!r}")
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    frac = m.group(7)
    microsecond = int((frac + "000000")[:6]) if frac else 0
    tz = timezone.utc
    zone = m.group(8)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        tz = timezone(sign * offset)
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=tz)


def format_iso_time(t: Optional[datetime]) -> Optional[str]:
    """ISO 8601 string in UTC with a ``Z`` suffix (``None`` stays ``None``)."""
    if t is None:
        return None
    t = ensure_utc(t)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond:06d}".rstrip("0") if t.microsecond else "") + "Z"


def ensure_utc(t: datetime) -> datetime:
    """Return an aware UTC datetime (naive input is assumed to be UTC)."""
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

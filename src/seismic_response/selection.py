"""Selecting channel epochs from parsed responses.

``select_channels`` follows evalresp's rules: a channel must match the
network/station/location/channel patterns (shell style globs) and, when a time
is given, its epoch must contain that time.  If several epochs of the same
channel match, evalresp keeps the *shortest* one containing the requested time,
or the *latest* one when no time is requested.
"""

from __future__ import annotations

import fnmatch
import itertools
import logging
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from .errors import ChannelNotFoundError
from .model import Channel
from .timeutil import ensure_utc

__all__ = ["select_channels", "select_channel", "split_patterns"]

log = logging.getLogger("seismic_response")

_Sncl = Tuple[str, str, str, str]  # network, station, location, channel patterns
_FOREVER = float("inf")


def split_patterns(value: Union[None, str, Iterable[str]]) -> List[str]:
    """Split a comma or space separated pattern list; ``None`` means match anything."""
    if value is None:
        return ["*"]
    if isinstance(value, str):
        parts = [p for p in value.replace(",", " ").split(" ") if p != ""]
        if not parts:
            # evalresp treats a blank location as the empty location code
            return [""] if value != "" and value.strip() == "" else ["*"]
        return parts
    return list(value)


def _glob(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def _location_pattern(p: str) -> str:
    # evalresp: all '?' means any location, all blanks means the empty location
    if p and set(p) == {"?"}:
        return "*"
    if p and p.strip() == "":
        return ""
    return p


def _matches(c: Channel, sncls: Sequence[_Sncl], time: Optional[datetime]) -> bool:
    if time is not None and not c.contains(time):
        return False
    if not sncls:
        return True
    for net, sta, loc, cha in sncls:
        net_ok = (not net and not c.network) or _glob(c.network, net) or (net == "??" and not c.network)
        if _glob(c.station, sta) and net_ok and _glob(c.location, _location_pattern(loc)) and _glob(c.channel, cha):
            return True
    return False


def _duration(c: Channel) -> float:
    if c.end_time is None or c.start_time is None:
        return _FOREVER
    return (c.end_time - c.start_time).total_seconds()


def _earlier(a: Channel, b: Channel) -> bool:
    """evalresp's ``earlier``: is ``a`` older than ``b`` (open-ended epochs are newest)."""
    open_a, open_b = a.end_time is None, b.end_time is None
    if open_a or open_b:
        if not open_b:
            return False
        if not open_a:
            return True
        return _start(a) <= _start(b)
    return a.end_time <= b.end_time


def _start(c: Channel) -> float:
    return c.start_time.timestamp() if c.start_time else -_FOREVER


def _same_channel(a: Channel, b: Channel) -> bool:
    return (a.network, a.station, a.location, a.channel) == (b.network, b.station, b.location, b.channel)


def select_channels(
    channels: Sequence[Channel],
    source_id: Optional[str] = None,
    *,
    network: Union[None, str, Iterable[str]] = None,
    station: Union[None, str, Iterable[str]] = None,
    location: Union[None, str, Iterable[str]] = None,
    channel: Union[None, str, Iterable[str]] = None,
    time: Optional[datetime] = None,
) -> List[Channel]:
    """Select channels by identifier and time (see module docstring for the rules).

    ``source_id`` is an FDSN Source Identifier glob such as ``FDSN:IU_ANMO_*``.
    ``network``, ``station``, ``location`` and ``channel`` are SEED code globs
    (comma or space separated lists allowed).  All filters combine with AND.
    """
    if time is not None:
        time = ensure_utc(time)

    sncls: List[_Sncl] = []
    if any(x is not None for x in (network, station, location, channel)):
        sncls = list(
            itertools.product(
                split_patterns(network), split_patterns(station), split_patterns(location), split_patterns(channel)
            )
        )
        # product yields (net, sta, loc, cha) in the order of the arguments
    return _select(channels, sncls, source_id, time)


def _select(
    channels: Sequence[Channel], sncls: Sequence[_Sncl], source_id: Optional[str], time: Optional[datetime]
) -> List[Channel]:
    pool: List[Optional[Channel]] = [c for c in channels if source_id is None or _glob(c.source_id, source_id)]
    result: List[Channel] = []
    warn = False
    for i, cand in enumerate(pool):
        if cand is None or not _matches(cand, sncls, time):
            continue
        best = True
        for j in range(i + 1, len(pool)):
            if not best:
                break
            other = pool[j]
            if other is None or not _same_channel(cand, other):
                continue
            if time is None:
                if _earlier(cand, other):
                    best = False
            else:
                if other.contains(time) and _duration(cand) >= _duration(other):
                    warn = True
                    best = False
            if best:
                pool[j] = None
        if best:
            result.append(cand)
        else:
            pool[i] = None
    if warn:
        log.warning("Two or more entries match the same SNCL and date; the shortest was used")
    return result


def select_channel(channels: Sequence[Channel], source_id: Optional[str] = None, **kwargs) -> Channel:
    """Like :func:`select_channels` but expects exactly one match.

    Raises :class:`~seismic_response.errors.ChannelNotFoundError` if nothing matches
    and ``ValueError`` if the selection is ambiguous.
    """
    found = select_channels(channels, source_id, **kwargs)
    if not found:
        raise ChannelNotFoundError("No channel matches the requested identifier and time")
    if len(found) > 1:
        names = ", ".join(c.source_id for c in found[:5])
        raise ValueError(f"Selection is ambiguous: {len(found)} channels match ({names}...)")
    return found[0]

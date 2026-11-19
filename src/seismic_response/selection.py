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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .errors import AmbiguousChannelError, ChannelNotFoundError
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

    ``source_id`` is an FDSN Source Identifier glob such as ``FDSN:IU_ANMO_*``; the
    ``FDSN:`` prefix is optional and matched without regard to case.
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


def _source_id_matches(c: Channel, pattern: str) -> bool:
    """Glob match against the Source ID, with or without its ``FDSN:`` prefix (in any case)."""
    if pattern[:5].upper() == "FDSN:":
        pattern = "FDSN:" + pattern[5:]
    sid = c.source_id
    return _glob(sid, pattern) or _glob(sid[5:], pattern)


def _select(
    channels: Sequence[Channel], sncls: Sequence[_Sncl], source_id: Optional[str], time: Optional[datetime]
) -> List[Channel]:
    matched = [c for c in channels if source_id is None or _source_id_matches(c, source_id)]
    pool: List[Optional[Channel]] = list(matched)
    # Only epochs of the same channel compete, so each channel's epochs are compared among themselves.
    same: Dict[Tuple[str, str, str, str], List[int]] = {}
    for i, c in enumerate(matched):
        same.setdefault((c.network, c.station, c.location, c.channel), []).append(i)
    chosen: List[Tuple[int, Channel]] = []
    warn = False
    for indices in same.values():
        for n, i in enumerate(indices):
            cand = pool[i]
            if cand is None or not _matches(cand, sncls, time):
                continue
            best = True
            for j in indices[n + 1 :]:
                if not best:
                    break
                other = pool[j]
                if other is None:
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
                chosen.append((i, cand))
            else:
                pool[i] = None
    result = [c for _, c in sorted(chosen, key=lambda x: x[0])]
    if warn:
        log.warning("Two or more entries match the same SNCL and date; the shortest was used")
    return result


def select_channel(channels: Sequence[Channel], source_id: Optional[str] = None, **kwargs) -> Channel:
    """Like :func:`select_channels` but expects exactly one match.

    Raises :class:`~seismic_response.errors.ChannelNotFoundError` if nothing matches
    and :class:`~seismic_response.errors.AmbiguousChannelError` if the selection is ambiguous.
    """
    found = select_channels(channels, source_id, **kwargs)
    if not found:
        raise ChannelNotFoundError("No channel matches the requested identifier and time")
    if len(found) > 1:
        names = ", ".join(c.source_id for c in found[:5])
        raise AmbiguousChannelError(f"Selection is ambiguous: {len(found)} channels match ({names}...)")
    return found[0]

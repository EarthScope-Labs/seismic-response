"""Parser for SAC poles-and-zeros (SAC PZ) files.

SAC PZ files are written by RDSEED and returned by the ``sacpz`` web service.  The
transfer function is ``H(s) = CONSTANT * prod(s - z) / prod(s - p)`` with
``s = 2*pi*i*f`` (``f`` in Hz, so poles and zeros are in rad/s).  The keyword
driven body looks like::

    ZEROS 3
    -7.853982e+01  +0.000000e+00
    POLES 2
    -1.207063e-02  +1.224561e-02
    -1.207063e-02  -1.224561e-02
    CONSTANT 3.816863e+11

``ZEROS n`` announces ``n`` zeros but zeros equal to (0, 0) may be left out, so
fewer lines may follow.  ``CONSTANT`` defaults to 1.  Optional ``*`` comment lines
before each block annotate it (``NETWORK``, ``STATION``, ``LOCATION``, ``CHANNEL``,
``START``, ``END``, ``SAMPLE RATE``, ``INPUT UNIT``, ``OUTPUT UNIT`` ...).  One file
may hold several such blocks (epochs and/or channels); each becomes a
:class:`~seismic_response.model.Channel` with a single poles-and-zeros stage.

The constant is converted to the package's model by evaluating the poles and zeros at
1 Hz: the stage gets ``a0 = 1/|PZ(1 Hz)|`` and a gain of ``CONSTANT*|PZ(1 Hz)|`` at
1 Hz, so that ``a0 * gain * PZ(s)`` equals ``CONSTANT * PZ(s)`` exactly and the
response has a meaningful sensitivity (output units per input unit at 1 Hz).
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Dict, List, Optional, Union

from .errors import ResponseParseError
from .model import LAPLACE_RADIANS, Channel, Decimation, Gain, PolesZeros, Stage
from .timeutil import parse_iso_time

__all__ = ["parse_sacpz", "looks_like_sacpz"]

MAX_POLES_ZEROS = 30
_KEYWORDS = ("ZEROS", "POLES", "CONSTANT")
_REFERENCE_FREQUENCY = 1.0  # Hz, where the constant is turned into a stage gain
_OPEN_END_YEAR = 2599  # end times this far in the future mean "no end"

# Names given to the units written in the annotations
_UNIT_NAMES = {
    "M": "M - Displacement in Meters",
    "M/S": "M/S - Velocity in Meters per Second",
    "M/S**2": "M/S**2 - Acceleration in Meters per Second per Second",
    "COUNTS": "COUNTS - Digital Counts",
}

# Station/channel codes from the RDSEED file name SAC_PZs_<NET>_<STA>_<CHA>[_<LOC>]...
_FILENAME_RE = re.compile(r"SAC_PZs_([^_]+)_([^_]+)_([^_.]+)(?:_([^_.]+))?")


def looks_like_sacpz(content: Union[str, bytes]) -> bool:
    """True if the first line that is not blank or a ``*`` comment starts with a SAC PZ keyword."""
    if isinstance(content, bytes):
        content = content[:4096].decode("utf-8", errors="replace")
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        tokens = line.split()
        return tokens[0].upper() in _KEYWORDS and len(tokens) >= 2
    return False


class _Block:
    def __init__(self) -> None:
        self.annotations: Dict[str, str] = {}
        self.zeros: List[complex] = []
        self.poles: List[complex] = []
        self.counts: Dict[str, int] = {}
        self.constant: Optional[float] = None
        self.current: Optional[str] = None  # "ZEROS" or "POLES" while collecting value lines
        self.has_data = False


def _float(token: str) -> float:
    return float(token.replace("D", "E").replace("d", "e"))


def parse_sacpz(content: Union[str, bytes], origin: str = "") -> List[Channel]:
    """Parse SAC PZ text into one :class:`Channel` per annotated block."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    where = origin or "SAC PZ"

    def fail(line_no: int, message: str) -> ResponseParseError:
        return ResponseParseError(f"{where}: line {line_no}: {message}")

    channels: List[Channel] = []
    block = _Block()

    def finish(line_no: int) -> None:
        nonlocal block
        if block.has_data:
            channels.append(_build_channel(block, origin, where, line_no))
        block = _Block()

    line_no = 0
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue

        if line.startswith("*"):
            if block.has_data:
                finish(line_no)
            key, sep, value = line.lstrip("*").strip().partition(":")
            if sep:
                key = re.sub(r"\(.*?\)", "", key)
                key = " ".join(key.upper().split())
                if key:
                    block.annotations[key] = value.strip()
            continue

        tokens = line.split()
        keyword = tokens[0].upper()
        if keyword in ("ZEROS", "POLES"):
            if keyword in block.counts:  # a repeated keyword starts the next (unannotated) block
                finish(line_no)
            try:
                count = int(tokens[1])
                if count < 0:
                    raise ValueError
            except (IndexError, ValueError):
                raise fail(line_no, f"{keyword} needs a non-negative integer count") from None
            if count > MAX_POLES_ZEROS:
                raise fail(line_no, f"{keyword} {count}: at most {MAX_POLES_ZEROS} are allowed")
            block.counts[keyword] = count
            block.current = keyword
            block.has_data = True
        elif keyword == "CONSTANT":
            try:
                block.constant = _float(tokens[1])
            except (IndexError, ValueError):
                raise fail(line_no, "CONSTANT needs a number") from None
            block.current = None
            block.has_data = True
        else:
            if block.current is None:
                raise fail(line_no, f"unrecognized line {line!r}")
            try:
                value = complex(_float(tokens[0]), _float(tokens[1]))
            except (IndexError, ValueError):
                raise fail(line_no, f"expected the real and imaginary part of a {block.current[:-1].lower()}") from None
            target = block.zeros if block.current == "ZEROS" else block.poles
            if len(target) >= block.counts[block.current]:
                raise fail(line_no, f"more values than the {block.counts[block.current]} announced by {block.current}")
            target.append(value)

    finish(line_no)
    if not channels:
        raise ResponseParseError(f"{where}: no ZEROS, POLES or CONSTANT found")
    return channels


def _build_channel(b: _Block, origin: str, where: str, line_no: int) -> Channel:
    nz, np_ = b.counts.get("ZEROS", 0), b.counts.get("POLES", 0)
    if len(b.poles) != np_:
        raise ResponseParseError(f"{where}: POLES {np_} announced but {len(b.poles)} listed (before line {line_no})")
    zeros = b.zeros + [0j] * (nz - len(b.zeros))  # unlisted zeros are (0, 0)
    constant = 1.0 if b.constant is None else b.constant

    gain, a0, a0_freq = Gain(1.0, 0.0), constant, 0.0
    ref = _magnitude(zeros, b.poles, _REFERENCE_FREQUENCY)
    if ref is not None:
        gain, a0, a0_freq = Gain(constant * ref, _REFERENCE_FREQUENCY), 1.0 / ref, _REFERENCE_FREQUENCY

    note = b.annotations
    stage = Stage(
        sequence=1,
        input_units=_unit(note.get("INPUT UNIT"), "M"),
        output_units=_unit(note.get("OUTPUT UNIT"), "COUNTS"),
        filter=PolesZeros(LAPLACE_RADIANS, zeros, list(b.poles), a0=a0, a0_freq=a0_freq),
        gain=gain,
    )
    rate = _positive_float(note.get("SAMPLE RATE"))
    if rate:
        stage.decimation = Decimation(input_sample_rate=rate, factor=1)

    network, station, location, channel = (note.get(k) for k in ("NETWORK", "STATION", "LOCATION", "CHANNEL"))
    m = _FILENAME_RE.search(origin or "")
    if m:  # codes from the RDSEED file name fill in what the annotations lack
        network, station, channel = network or m.group(1), station or m.group(2), channel or m.group(3)
        location = location if location is not None else m.group(4)
    return Channel(
        network=_code(network),
        station=_code(station) or "SACPZ",
        location=_code(location),
        channel=_code(channel),
        start_time=_time(note.get("START"), where),
        end_time=_time(note.get("END"), where, is_end=True),
        stages=[stage],
        origin=origin,
    )


def _magnitude(zeros: List[complex], poles: List[complex], freq: float) -> Optional[float]:
    """``|prod(s-z)/prod(s-p)|`` at ``s = 2*pi*i*freq``; ``None`` if it is not usable."""
    s = complex(0.0, 2 * math.pi * freq)
    total = 0.0
    for z in zeros:
        d = abs(s - z)
        if d == 0.0:
            return None
        total += math.log(d)
    for p in poles:
        d = abs(s - p)
        if d == 0.0:
            return None
        total -= math.log(d)
    try:
        ref = math.exp(total)
    except OverflowError:
        return None
    return ref if 0.0 < ref < math.inf else None


def _unit(value: Optional[str], default: str) -> str:
    value = (value or "").strip() or default
    return _UNIT_NAMES.get(value.upper(), value)


def _code(value: Optional[str]) -> str:
    value = (value or "").strip()
    return "" if value == "--" else value


def _positive_float(value: Optional[str]) -> Optional[float]:
    try:
        number = _float((value or "").split()[0])
    except (IndexError, ValueError):
        return None
    return number if number > 0 else None


def _time(value: Optional[str], where: str, is_end: bool = False) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        t = parse_iso_time(value)
    except ValueError:
        raise ResponseParseError(f"{where}: cannot parse time {value!r}") from None
    return None if is_end and t.year >= _OPEN_END_YEAR else t

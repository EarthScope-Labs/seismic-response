"""Parser for SEED RESP text (as written by rdseed and evalresp's StationXML converter).

The parser is organized around *blockettes*.  A RESP line looks like::

    B053F07     A0 normalization factor:               86083
    B053F10-13    0  0.000000E+00  0.000000E+00  0.000000E+00  0.000000E+00

i.e. ``B<blockette>F<field>[-<last field>]`` followed by either a ``label: value``
pair or whitespace separated data columns.  Every supported blockette begins with
its ``F03`` field, which is how blockettes are delimited here.

Supported blockettes: 50/52 (channel header), 53 (poles & zeros), 54 (FIR and
IIR coefficients), 55 (response list), 56 (generic, ignored), 57 (decimation),
58 (gain / sensitivity), 61 (FIR) and 62 (polynomial).  RESP "dictionary"
references (blockette 60 and the blockette 4x lookups) are not supported.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .errors import ResponseParseError
from .model import (
    FIR,
    LAPLACE_HERTZ,
    LAPLACE_RADIANS,
    Z_TRANSFORM,
    Channel,
    Decimation,
    Gain,
    Generic,
    IIRCoefficients,
    PolesZeros,
    Polynomial,
    ResponseList,
    Stage,
)

__all__ = ["parse_resp", "parse_resp_time", "looks_like_resp"]

_LINE_RE = re.compile(r"^B(\d{3})F(\d{2})(?:-(\d{2}))?\s*(.*?)\s*$")

# Blockettes that begin a stage-level element, each starting at its F03 line
_STAGE_BLOCKETTES = (53, 54, 55, 56, 57, 58, 61, 62)

# Field holding the stage sequence number
_SEQUENCE_FIELD = {53: 4, 54: 4, 55: 3, 56: 3, 57: 3, 58: 3, 61: 3, 62: 4}

# Fields holding (input units, output units) for filter blockettes
_UNITS_FIELDS = {53: (5, 6), 54: (5, 6), 55: (4, 5), 56: (4, 5), 61: (6, 7), 62: (5, 6)}


def looks_like_resp(text: str) -> bool:
    """True if the text contains RESP style ``B###F##`` lines."""
    return re.search(r"^B0\d\dF\d\d", text, re.MULTILINE) is not None


def parse_resp_time(value: str) -> Optional[datetime]:
    """Parse a RESP time (``YYYY,DDD[,HH[:MM[:SS[.ffff]]]]``) to an aware UTC datetime.

    ``"No Ending Time"`` (any case) and empty strings return ``None``.
    """
    value = value.strip()
    if not value or value.lower().startswith("no ending time"):
        return None
    parts = value.split(",")
    try:
        year = int(parts[0])
        jday = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 1
        hour = minute = 0
        second = 0.0
        if len(parts) > 2 and parts[2].strip():
            hms = parts[2].strip().split(":")
            hour = int(hms[0])
            if len(hms) > 1 and hms[1]:
                minute = int(hms[1])
            if len(hms) > 2 and hms[2]:
                second = float(hms[2])
        base = datetime(year, 1, 1, tzinfo=timezone.utc)
        return base + timedelta(days=jday - 1, hours=hour, minutes=minute, seconds=second)
    except (ValueError, IndexError, OverflowError) as exc:
        raise ValueError(f"Cannot parse RESP time {value!r}") from exc


class _Block:
    """Lines of one blockette, keyed by (first) field number."""

    def __init__(self, number: int, line_no: int):
        self.number = number
        self.line_no = line_no
        self.fields: Dict[int, List[str]] = {}

    def add(self, field: int, content: str) -> None:
        self.fields.setdefault(field, []).append(content)

    def has(self, field: int) -> bool:
        return field in self.fields

    def value(self, field: int, default: Optional[str] = None) -> str:
        """The text after the first ``:`` of a labelled field."""
        try:
            raw = self.fields[field][0]
        except KeyError:
            if default is not None:
                return default
            raise ResponseParseError(
                f"Blockette {self.number} field {field:02d} missing "
                f"(blockette starting at RESP line {self.line_no})"
            ) from None
        idx = raw.find(":")
        return (raw[idx + 1 :] if idx >= 0 else raw).strip()

    def number_value(self, field: int, kind=float, default=None):
        if default is not None and field not in self.fields:
            return default
        text = self.value(field)
        token = text.split()[0] if text.split() else ""
        try:
            return kind(token)
        except ValueError:
            raise ResponseParseError(
                f"Blockette {self.number} field {field:02d}: cannot convert {text!r} to "
                f"{kind.__name__} (RESP line {self.line_no})"
            ) from None

    def rows(self, field: int) -> List[List[str]]:
        """Whitespace separated columns of every line for a data field."""
        return [line.split() for line in self.fields.get(field, [])]


def parse_resp(text: str, origin: str = "") -> List[Channel]:
    """Parse RESP text into a list of :class:`~seismic_response.model.Channel` epochs.

    Raises :class:`~seismic_response.errors.ResponseParseError` for malformed or
    unsupported content.
    """
    channels: List[Channel] = []
    chan: Optional[Channel] = None
    block: Optional[_Block] = None

    def flush() -> None:
        nonlocal block
        if block is not None and chan is not None:
            _apply_block(chan, block)
        block = None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw or raw[0] == "#":
            continue
        m = _LINE_RE.match(raw.replace("\t", " ").rstrip("\r"))
        if not m:
            continue
        blkt, fld, content = int(m.group(1)), int(m.group(2)), m.group(4)

        if blkt == 50:
            if fld == 3:
                flush()
                chan = Channel(network="", station=_after_colon(content), location="", channel="", origin=origin)
                channels.append(chan)
            elif fld == 16 and chan is not None:
                net = _after_colon(content)
                chan.network = "" if net.startswith("??") else net
            continue

        if blkt == 52:
            if chan is None:
                continue
            if fld == 3:
                loc = _after_colon(content)
                chan.location = "" if loc.startswith("??") else loc
            elif fld == 4:
                chan.channel = _after_colon(content)
            elif fld == 22:
                chan.start_time = _time_or_error(_after_colon(content), line_no)
            elif fld == 23:
                chan.end_time = _time_or_error(_after_colon(content), line_no)
            continue

        if blkt == 60 or 41 <= blkt <= 48 or blkt in (31, 33, 34, 35):
            if chan is not None and blkt in (60,):
                raise ResponseParseError(
                    f"RESP line {line_no}: dictionary references (blockette 60) are not supported"
                )
            continue

        if blkt not in _STAGE_BLOCKETTES or chan is None:
            continue

        if fld == 3:
            flush()
            block = _Block(blkt, line_no)
        if block is None or block.number != blkt:
            # Data lines without a start-of-blockette line; evalresp would error
            raise ResponseParseError(f"RESP line {line_no}: blockette {blkt} field {fld:02d} out of place")
        block.add(fld, content)

    flush()

    if not channels:
        raise ResponseParseError("No channel (B050F03) information found in RESP input")
    return channels


def _after_colon(content: str) -> str:
    idx = content.find(":")
    return (content[idx + 1 :] if idx >= 0 else content).strip()


def _time_or_error(value: str, line_no: int) -> Optional[datetime]:
    try:
        return parse_resp_time(value)
    except ValueError as exc:
        raise ResponseParseError(f"RESP line {line_no}: {exc}") from None


def _stage_for(chan: Channel, sequence: int) -> Stage:
    """The stage to which a blockette with this sequence number belongs.

    As in evalresp, blockettes join the stage only when they follow directly
    after blockettes of the same sequence number.
    """
    if chan.stages and chan.stages[-1].sequence == sequence:
        return chan.stages[-1]
    stage = Stage(sequence=sequence)
    chan.stages.append(stage)
    return stage


def _apply_block(chan: Channel, b: _Block) -> None:
    n = b.number
    sequence = b.number_value(_SEQUENCE_FIELD[n], int)
    stage = _stage_for(chan, sequence)

    if n == 57:
        stage.decimation = Decimation(
            input_sample_rate=b.number_value(4, float),
            factor=int(b.number_value(5, float)),
            offset=int(b.number_value(6, float)),
            estimated_delay=b.number_value(7, float),
            correction_applied=b.number_value(8, float),
        )
        return

    if n == 58:
        stage.gain = Gain(value=b.number_value(4, float), frequency=b.number_value(5, float))
        return

    new_filter, in_units, out_units = _build_filter(b)
    _attach_filter(stage, new_filter, in_units, out_units, b)


def _build_filter(b: _Block) -> Tuple[object, Optional[str], Optional[str]]:
    n = b.number
    in_f, out_f = _UNITS_FIELDS[n]
    in_units = b.value(in_f, "")
    out_units = b.value(out_f, "")

    if n == 53:
        kind = _type_code(b)
        try:
            kind = {"A": LAPLACE_RADIANS, "B": LAPLACE_HERTZ, "D": Z_TRANSFORM}[kind]
        except KeyError:
            raise ResponseParseError(
                f"RESP line {b.line_no}: unsupported poles & zeros transfer function type {kind!r}"
            ) from None
        nzeros = b.number_value(9, int)
        npoles = b.number_value(14, int)
        zeros = _complex_rows(b, 10, nzeros, "zero")
        poles = _complex_rows(b, 15, npoles, "pole")
        return (
            PolesZeros(
                kind=kind,
                zeros=zeros,
                poles=poles,
                a0=b.number_value(7, float),
                a0_freq=b.number_value(8, float),
            ),
            in_units,
            out_units,
        )

    if n == 54:
        code = _type_code(b)
        if code != "D":
            raise ResponseParseError(
                f"RESP line {b.line_no}: unsupported coefficient filter type {code!r}; "
                "analog filters should be represented as poles and zeros (blockette 53)"
            )
        nnum = b.number_value(7, int)
        nden = b.number_value(10, int)
        numerators = _real_rows(b, 8, nnum, "numerator")
        if nden == 0:
            return FIR("A", numerators), in_units, out_units
        denominators = _real_rows(b, 11, nden, "denominator")
        return IIRCoefficients(numerators, denominators), in_units, out_units

    if n == 55:
        count = b.number_value(6, int)
        rows = b.rows(7)
        if len(rows) < count:
            raise ResponseParseError(
                f"RESP line {b.line_no}: response list declares {count} responses but has {len(rows)}"
            )
        freqs, amps, phases = [], [], []
        for row in rows[:count]:
            # Rows are "i freq amp amp_err phase phase_err" or without the leading index
            offset = len(row) - 5
            if offset not in (0, 1):
                raise ResponseParseError(f"RESP line {b.line_no}: unknown response list row format {row}")
            try:
                freqs.append(float(row[offset]))
                amps.append(float(row[1 + offset]))
                phases.append(float(row[3 + offset]))
            except (ValueError, IndexError):
                raise ResponseParseError(f"RESP line {b.line_no}: bad response list row {row}") from None
        return ResponseList(freqs, amps, phases), in_units, out_units

    if n == 56:
        return Generic(), in_units, out_units

    if n == 61:
        code = b.value(5)[:1]
        if code not in ("A", "B", "C"):
            raise ResponseParseError(f"RESP line {b.line_no}: unexpected FIR symmetry code {code!r}")
        count = b.number_value(8, int)
        rows = b.rows(9)
        if len(rows) < count:
            raise ResponseParseError(
                f"RESP line {b.line_no}: FIR declares {count} coefficients but has {len(rows)}"
            )
        try:
            coeffs = [float(r[1]) for r in rows[:count]]
        except (ValueError, IndexError):
            raise ResponseParseError(f"RESP line {b.line_no}: bad FIR coefficient line") from None
        return FIR(code, coeffs), in_units, out_units

    if n == 62:
        if _type_code(b) != "P":
            raise ResponseParseError(f"RESP line {b.line_no}: unexpected polynomial type")
        count = b.number_value(14, int)
        rows = b.rows(15)
        if len(rows) < count:
            raise ResponseParseError(
                f"RESP line {b.line_no}: polynomial declares {count} coefficients but has {len(rows)}"
            )
        try:
            coeffs = [float(r[1]) for r in rows[:count]]
        except (ValueError, IndexError):
            raise ResponseParseError(f"RESP line {b.line_no}: bad polynomial coefficient line") from None
        return (
            Polynomial(
                coefficients=coeffs,
                approximation_type=b.value(7, "M")[:1],
                frequency_units=b.value(8, "B")[:1],
                lower_freq_bound=b.number_value(9, float, 0.0),
                upper_freq_bound=b.number_value(10, float, 0.0),
                lower_approx_bound=b.number_value(11, float, 0.0),
                upper_approx_bound=b.number_value(12, float, 0.0),
                max_abs_error=b.number_value(13, float, 0.0),
            ),
            in_units,
            out_units,
        )

    raise AssertionError(f"unhandled blockette {n}")  # pragma: no cover


def _type_code(b: _Block) -> str:
    text = b.value(3)
    return text.split()[0] if text.split() else ""


def _complex_rows(b: _Block, field: int, count: int, what: str) -> List[complex]:
    rows = b.rows(field)
    if len(rows) < count:
        raise ResponseParseError(
            f"RESP line {b.line_no}: {count} {what}s declared but {len(rows)} found"
        )
    out = []
    for row in rows[:count]:
        try:
            out.append(complex(float(row[1]), float(row[2])))
        except (ValueError, IndexError):
            raise ResponseParseError(f"RESP line {b.line_no}: bad {what} line {row}") from None
    return out


def _real_rows(b: _Block, field: int, count: int, what: str) -> List[float]:
    rows = b.rows(field)
    if len(rows) < count:
        raise ResponseParseError(
            f"RESP line {b.line_no}: {count} {what}s declared but {len(rows)} found"
        )
    try:
        return [float(r[1]) for r in rows[:count]]
    except (ValueError, IndexError):
        raise ResponseParseError(f"RESP line {b.line_no}: bad {what} line") from None


def _attach_filter(stage: Stage, new, in_units, out_units, b: _Block) -> None:
    """Attach a filter to a stage, merging continuation blockettes (FIR, list)."""
    current = stage.filter
    if current is None:
        stage.filter = new
        # Units come from the first filter blockette of the stage
        stage.input_units = in_units
        stage.output_units = out_units
        return
    if isinstance(current, FIR) and isinstance(new, FIR) and current.symmetry == new.symmetry:
        current.coefficients.extend(new.coefficients)
        return
    if isinstance(current, ResponseList) and isinstance(new, ResponseList):
        current.frequencies.extend(new.frequencies)
        current.amplitudes.extend(new.amplitudes)
        current.phases.extend(new.phases)
        return
    raise ResponseParseError(
        f"RESP line {b.line_no}: more than one filter type in stage {stage.sequence}"
    )

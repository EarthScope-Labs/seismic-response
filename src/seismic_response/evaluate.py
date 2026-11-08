"""Evaluation of instrument responses.

This module is a port of the response *evaluation* logic of the C evalresp
program (``check_channel``, ``normalize_response`` and ``calculate_response``),
which is the reference that results must match.  Where evalresp does something
unusual (for example the sign conventions of its Z-transform handling, or the
way it renormalizes a stage's gain to the sensitivity frequency) that behavior
is reproduced deliberately and called out in comments.

Frequency response conventions
------------------------------
* Laplace-domain stages are evaluated at ``s = i*omega`` where ``omega`` is
  ``2*pi*f`` (rad/s poles and zeros) or ``f`` (Hertz poles and zeros).
* The result is the complex response in counts (or the output unit of the last
  stage) per unit of the *requested* ground motion unit.  By default evalresp
  reports velocity (``m/s``), but this package defaults to ``unit="def"``, the
  unit documented in the response; pass ``unit="vel"`` for evalresp's behavior.
"""

from __future__ import annotations

import bisect
import copy
import logging
import math
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import units as U
from .errors import ResponseValueError
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
)

__all__ = [
    "evaluate",
    "generate_frequencies",
    "ResponseResult",
    "ResponseInfo",
    "StageInfo",
    "phase_degrees",
    "unwrap_phase",
]

log = logging.getLogger("seismic_response")

_FIR_NORM_TOL = 0.02  # FIR_NORM_TOL in evalresp
_RANGE_TOL = 1e-9  # relative slack on a response list's frequency limits

# Stage categories (evalresp's *_TYPE constants)
_GAIN_ONLY = "gain"
_PZ = "pz"
_IIR_PZ = "iir_pz"
_FIR = "fir"
_IIR = "iir"
_LIST = "list"
_POLY = "poly"
_GENERIC = "generic"
_NOFILTER = "none"

_UNIT_OPTIONS = {"def": U.DEFAULT, "dis": U.DIS, "vel": U.VEL, "acc": U.ACC}


# --------------------------------------------------------------------------- results


@dataclass
class StageInfo:
    """Summary of a stage as evaluated (after normalization)."""

    sequence: int
    kind: str
    gain: Optional[float] = None
    gain_frequency: Optional[float] = None
    a0: Optional[float] = None
    description: str = ""
    sample_interval: Optional[float] = None
    selected: bool = True
    summary: str = ""  # evalresp-style one line description (for verbose output)


@dataclass
class ResponseInfo:
    """Details of how a response was computed (mirrors evalresp's verbose output)."""

    input_units: Optional[str]
    output_units: Optional[str]
    requested_unit: str
    reported_sensitivity: float
    calculated_sensitivity: float
    sensitivity_frequency: float
    calculated_delay: float
    estimated_delay: float
    correction_applied: float
    sample_interval: float
    stages: List[StageInfo] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ResponseResult:
    """A computed response.

    ``frequencies`` may differ from those requested when a selected stage is a
    response list (blockette 55): evalresp then reports exactly the listed
    frequencies.
    """

    channel: Channel
    frequencies: List[float]
    values: List[complex]
    unit: str
    info: ResponseInfo
    stage_values: Optional[Dict[int, List[complex]]] = None

    @property
    def amplitudes(self) -> List[float]:
        return [math.hypot(v.real, v.imag) for v in self.values]

    def phases(self, unwrap: bool = False) -> List[float]:
        """Phase in degrees; ``unwrap=True`` removes 360 degree jumps as evalresp does."""
        return phase_degrees(self.values, unwrap=unwrap)

    def __len__(self) -> int:
        return len(self.frequencies)


def phase_degrees(values: Sequence[complex], unwrap: bool = False) -> List[float]:
    """Phase (degrees) of complex values, following evalresp's output code.

    evalresp adds ``1e-200`` to the real part inside ``atan2`` so that an exact
    zero response yields a defined phase.  With ``unwrap`` the first phase is
    forced into [0, 360) and subsequent values are unwrapped by ±360.
    """
    phases = [math.atan2(v.imag, v.real + 1.0e-200) * 180.0 / math.pi for v in values]
    if unwrap and phases:
        added = 0.0
        while phases[0] + added < 0:
            added += 360.0
        while phases[0] + added >= 360.0:
            added -= 360.0
        prev = phases[0] + added
        for i, p in enumerate(phases):
            p, added = unwrap_phase(p, prev, 360.0, added)
            phases[i] = p
            prev = p
    return phases


def unwrap_phase(phase: float, prev_phase: float, rng: float, added: float) -> Tuple[float, float]:
    """One step of evalresp's phase unwrapping; returns ``(phase, added_value)``."""
    phase += added
    if abs(phase - prev_phase) > rng / 2:
        if phase - prev_phase > 0:
            added -= rng
            phase -= rng
        else:
            added += rng
            phase += rng
    return phase, added


# --------------------------------------------------------------------------- frequencies


def generate_frequencies(
    minimum: float = 1.0,
    maximum: Optional[float] = None,
    count: int = 1,
    linear: bool = False,
) -> List[float]:
    """Frequency vector with evalresp's rules (log or linear spacing).

    A negative/absent ``maximum`` defaults to ``minimum``; if the limits are
    reversed they are swapped.

    >>> [round(f, 6) for f in generate_frequencies(0.01, 10, 3)]
    [0.01, 0.316228, 10.0]
    """
    for v in (minimum, maximum):
        if v is not None and not math.isfinite(v):
            raise ResponseValueError("Frequencies must be finite numbers")
    if minimum < 0:
        minimum = 1.0
    if maximum is None or maximum < 0:
        maximum = minimum
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    if not linear and minimum == 0:
        raise ResponseValueError("Cannot have zero frequency with logarithmic spacing")
    if count < 1:
        raise ResponseValueError("Number of frequencies must be at least 1")

    if linear:
        lo, hi = minimum, maximum
    else:
        lo, hi = math.log10(minimum), math.log10(maximum)
    delta = 0.0 if count == 1 else (hi - lo) / (count - 1)
    out = []
    for i in range(count):
        v = lo + i * delta
        out.append(v if linear else math.pow(10.0, v))
    return out


# --------------------------------------------------------------------------- working copies


@dataclass
class _WStage:
    """Mutable working copy of a stage (normalization adjusts gains and A0/H0)."""

    seq: int
    kind: str
    in_class: int
    out_class: int
    filter: object
    deci: Optional[Decimation]
    gain: Optional[Gain]
    in_units: Optional[str]
    out_units: Optional[str]
    nc: int = 0  # number of FIR coefficients (full filter)

    @property
    def sint(self) -> float:
        return self.deci.sample_interval if self.deci is not None else 0.0


@dataclass
class _Work:
    stages: List[_WStage]
    sensit: float = 0.0
    sensfreq: float = 0.0
    calc_sensit: float = 0.0
    calc_delay: float = 0.0
    estim_delay: float = 0.0
    applied_corr: float = 0.0
    sint: float = 0.0
    unit_scale: float = 1.0
    first_units_class: int = U.DEFAULT
    warnings: List[str] = field(default_factory=list)


class _Options:
    def __init__(self, unit, stages, use_total_sensitivity, use_estimated_delay, b62_x, exclude=None):
        key = (unit or "def").lower()
        if key not in _UNIT_OPTIONS:
            raise ValueError(f"Unit must be one of 'def', 'dis', 'vel', 'acc'; got {unit!r}")
        self.unit_key = key
        self.unit = _UNIT_OPTIONS[key]
        self.start, self.stop = stages if stages is not None else (None, None)
        self.use_total_sensitivity = use_total_sensitivity
        self.use_estimated_delay = use_estimated_delay
        self.b62_x = b62_x
        self.exclude = frozenset(int(x) for x in exclude) if exclude else frozenset()

    def selects(self, seq: int) -> bool:
        """Whether a stage takes part in the calculation (stage 0 never contributes)."""
        if seq in self.exclude:
            return False
        if self.start is None and self.stop is None:
            return True
        lo = self.start if self.start is not None else 0
        return seq >= lo and (self.stop is None or seq <= self.stop)


def _warn(work: _Work, message: str) -> None:
    work.warnings.append(message)
    log.warning(message)


# --------------------------------------------------------------------------- check_channel


def _check_symmetry(fir: FIR, ident: str, work: _Work) -> None:
    """Normalize (if needed) and detect symmetry of an asymmetric FIR, as evalresp does."""
    coeffs = fir.coefficients
    nc = len(coeffs)
    total = 0.0
    for c in coeffs:  # plain running sum, as in C (sum() may use compensated summation)
        total += c
    if nc and (total < 1.0 - _FIR_NORM_TOL or total > 1.0 + _FIR_NORM_TOL):
        if total == 0.0:
            raise ResponseValueError(f"{ident}: FIR coefficients sum to zero; cannot normalize")
        _warn(work, f"{ident}: FIR normalized: sum[coef]={total:E}")
        fir.coefficients = coeffs = [c / total for c in coeffs]

    if nc % 2 == 0:
        n0 = nc // 2
        for k in range(n0):
            if coeffs[n0 + k] != coeffs[n0 - k - 1]:
                return
        fir.symmetry = "C"
        fir.coefficients = coeffs[:n0]
    else:
        n0 = (nc - 1) // 2
        for k in range(1, nc - n0):
            if coeffs[n0 + k] != coeffs[n0 - k]:
                return
        fir.symmetry = "B"
        fir.coefficients = coeffs[: nc - n0]


def _fir_ncoeffs(fir: FIR) -> int:
    n = len(fir.coefficients)
    return n * 2 - 1 if fir.symmetry == "B" else (n * 2 if fir.symmetry == "C" else n)


def _prepare(channel: Channel, opts: _Options) -> _Work:
    """Port of evalresp ``check_channel`` operating on a private copy of the channel."""
    ident = channel.nslc
    work = _Work(stages=[])
    stages_in = [copy.deepcopy(s) for s in channel.stages]
    if not stages_in:
        raise ResponseValueError(f"{ident}: channel has no response stages")

    # A requested motion unit needs the first numbered stage that documents input units (not stage 0)
    # to be ground motion; its unit scale applies to the whole response.
    if opts.unit != U.DEFAULT:
        text = next((s.input_units for s in stages_in if s.sequence != 0 and s.input_units), None)
        unit_class, scale = U.classify_units(text)
        if unit_class not in (U.DIS, U.VEL, U.ACC):
            raise ResponseValueError(
                f"{ident}: input units ('{text or 'none documented'}') are not ground motion; "
                f"cannot convert to {U.motion_unit_name(opts.unit)}"
            )
        work.first_units_class = unit_class
        work.unit_scale = scale

    prev: Optional[_WStage] = None
    for st in stages_in:
        f = st.filter
        kind = _NOFILTER
        nc = 0

        if isinstance(f, Polynomial):
            kind = _POLY
        elif isinstance(f, PolesZeros):
            kind = _IIR_PZ if f.kind == Z_TRANSFORM else _PZ
        elif isinstance(f, ResponseList):
            kind = _LIST
        elif isinstance(f, Generic):
            kind = _GENERIC
            _warn(work, f"{ident}: GENERIC blockette is detected in stage {st.sequence}; content is ignored")
        elif isinstance(f, FIR):
            kind = _FIR
            if f.symmetry == "A":
                _check_symmetry(f, ident, work)
            nc = _fir_ncoeffs(f)
        elif isinstance(f, IIRCoefficients):
            kind = _IIR
            nc = 1  # makes the calculated delay 0, as in evalresp

        if st.gain is not None:
            if st.sequence == 0:
                work.sensit = st.gain.value
                work.sensfreq = st.gain.frequency
            if kind == _NOFILTER:
                kind = _GAIN_ONLY

        if st.decimation is not None:
            if f is None:
                raise ResponseValueError(
                    f"{ident}: DECIMATION blockette with no associated filter at stage {st.sequence}"
                )
            d = st.decimation
            if kind == _FIR and nc > 0:
                work.calc_delay += ((nc - 1) / 2.0) * d.sample_interval
            work.estim_delay += d.estimated_delay
            work.applied_corr += d.correction_applied
            work.sint = d.sample_interval * d.factor

        if opts.unit == U.DEFAULT:
            in_class = out_class = U.DEFAULT if f is not None else U.UNDEF
        else:
            in_class = U.classify_units(st.input_units)[0]
            out_class = U.classify_units(st.output_units)[0]
            if st.input_units is not None and in_class == U.UNDEF:
                _warn(work, f"units found ('{st.input_units}') cannot be converted to {U.motion_unit_name(opts.unit)}")
            if st.output_units is not None and out_class == U.UNDEF:
                _warn(work, f"units found ('{st.output_units}') cannot be converted to {U.motion_unit_name(opts.unit)}")

        gain = st.gain
        if kind != _GAIN_ONLY:
            if in_class == U.UNDEF or out_class == U.UNDEF:
                raise ResponseValueError(f"{ident}: undefined units in stage {st.sequence}")
            if gain is None and kind != _POLY:
                _warn(work, f"{ident}: Missing gain in stage {st.sequence} - using unit gain at 1Hz")
                # evalresp only attaches the substitute gain when a decimation blockette is present
                if st.decimation is not None:
                    gain = Gain(1.0, 1.0)

        ws = _WStage(
            seq=st.sequence,
            kind=kind,
            in_class=in_class,
            out_class=out_class,
            filter=f,
            deci=st.decimation,
            gain=gain,
            in_units=st.input_units,
            out_units=st.output_units,
            nc=nc,
        )

        if kind in (_PZ, _FIR, _IIR_PZ, _IIR, _LIST):
            if prev is not None and prev.out_class != in_class:
                raise ResponseValueError(f"{ident}: units mismatch between stages")

        if kind in (_IIR_PZ, _FIR, _IIR) and st.decimation is None:
            raise ResponseValueError(
                f"{ident}: required decimation blockette for IIR or FIR filter missing (stage {st.sequence})"
            )

        if kind != _GAIN_ONLY and st.sequence:
            prev = ws
        work.stages.append(ws)

    return work


# --------------------------------------------------------------------------- transforms


def _div(a: float, b: float) -> float:
    """IEEE style division (C semantics) so a pole on the axis gives inf/nan, not an exception."""
    if b == 0.0:
        if a == 0.0 or a != a:
            return math.nan
        return math.copysign(math.inf, a)
    return a / b


def _is_infinite(value: complex) -> bool:
    return math.isinf(value.real)


def _analog_trans(pz: PolesZeros, freq: float) -> complex:
    """Laplace transform response of poles/zeros (evalresp ``analog_trans``).

    Zeros and poles at the evaluation point cancel pairwise; excess zeros give 0 and
    excess poles ``inf`` (see ``_is_infinite``).
    """
    if pz.kind == LAPLACE_RADIANS:
        freq = 2 * math.pi * freq
    omega = complex(0.0, freq)
    # evalresp starts both products at 1+1i; the common factor cancels in the ratio.
    num = complex(1.0, 1.0)
    den = complex(1.0, 1.0)
    zeros_at_omega = poles_at_omega = 0
    for z in pz.zeros:
        d = omega - z
        if d == 0:
            zeros_at_omega += 1
        else:
            num *= d
    for p in pz.poles:
        d = omega - p
        if d == 0:
            poles_at_omega += 1
        else:
            den *= d
    if zeros_at_omega != poles_at_omega:
        return complex(0.0, 0.0) if zeros_at_omega > poles_at_omega else complex(math.inf, 0.0)
    mod_sq = den.real * den.real + den.imag * den.imag
    t = complex(den.real, -den.imag) * num
    return complex(pz.a0 * _div(t.real, mod_sq), pz.a0 * _div(t.imag, mod_sq))


def _iir_pz_trans(pz: PolesZeros, w: float, sint: float) -> complex:
    """Z-transform poles/zeros (evalresp ``iir_pz_trans``).

    evalresp evaluates at ``c + i*s`` with ``c = cos(w*T)`` and ``s = sin(w*T)``
    (i.e. the point on the unit circle) using separate magnitude and phase
    accumulation; that is reproduced here exactly.
    """
    wsint = w * sint
    c = math.cos(wsint)
    s = math.sin(wsint)
    mod = 1.0
    pha = 0.0
    for z in pz.zeros:
        r = c - z.real
        i = s - z.imag
        mod *= math.sqrt(r * r + i * i)
        if not (r == 0.0 and i == 0.0):
            pha += math.atan2(i, r)
    for p in pz.poles:
        r = c - p.real
        i = s - p.imag
        mod = _div(mod, math.sqrt(r * r + i * i))
        if not (r == 0.0 and i == 0.0):
            pha -= math.atan2(i, r)
    return complex(mod * math.cos(pha) * pz.a0, mod * math.sin(pha) * pz.a0)


def _fir_sym_trans(fir: FIR, w: float, sint: float) -> complex:
    a = fir.coefficients
    na = len(a)
    wsint = w * sint
    r = 0.0
    if fir.symmetry == "B":
        for k in range(na - 1):
            r += a[k] * math.cos(wsint * (na - (k + 1)))
        return complex((a[na - 1] + 2.0 * r) * fir.h0, 0.0)
    for k in range(na):
        r += a[k] * math.cos(wsint * ((na - (k + 1)) + 0.5))
    return complex(2.0 * r * fir.h0, 0.0)


def _fir_asym_trans(fir: FIR, w: float, sint: float) -> complex:
    a = fir.coefficients
    na = len(a)
    wsint = w * sint

    # All coefficients equal: closed form (evalresp special case; note H0 is not applied)
    if na and all(a[k] == a[0] for k in range(1, na)):
        if wsint == 0.0:
            return complex(1.0, 0.0)
        return complex(_div(math.sin(wsint / 2.0 * na), math.sin(wsint / 2.0)) * a[0], 0.0)

    r = i = 0.0
    for k in range(na):
        y = wsint * k
        r += a[k] * math.cos(y)
        i += a[k] * -math.sin(y)
    mod = math.sqrt(r * r + i * i)
    pha = math.atan2(i, r) + (w * ((na - 1) / 2.0) * sint)
    return complex(mod * math.cos(pha) * fir.h0, mod * math.sin(pha) * fir.h0)


def _iir_trans(iir: IIRCoefficients, w: float, sint: float) -> complex:
    wt = w * sint

    def part(c: List[float]) -> Tuple[float, float]:
        xre, xim = c[0], 0.0
        for k in range(1, len(c)):
            xre += c[k] * math.cos(-(k * wt))
            xim += c[k] * math.sin(-(k * wt))
        return xre, xim

    nre, nim = part(iir.numerators)
    dre, dim = part(iir.denominators)
    amp = _div(math.sqrt(nre * nre + nim * nim), math.sqrt(dre * dre + dim * dim))
    phase = math.atan2(nim, nre) - math.atan2(dim, dre)
    return complex(amp * math.cos(phase) * iir.h0, amp * math.sin(phase) * iir.h0)


def _polynomial(poly: Polynomial, x: float) -> complex:
    """First derivative of the MacLaurin polynomial at ``x``; a negative slope has phase 180 degrees."""
    amp = 0.0
    for j in range(1, len(poly.coefficients)):
        amp += poly.coefficients[j] * j * math.pow(x, j - 1)
    return complex(amp, 0.0)


def _time_shift(delay: float, w: float) -> complex:
    return complex(math.cos(w * delay), math.sin(w * delay))


def _filter_response(ws: _WStage, freq: float, index: int, opts: _Options, ident: str) -> Optional[complex]:
    """Response of a stage's filter (and decimation delay) at one frequency, or ``None``."""
    w = 2 * math.pi * freq
    f = ws.filter
    kind = ws.kind
    out: Optional[complex] = None

    if kind == _PZ:
        out = _analog_trans(f, freq)
    elif kind == _IIR_PZ:
        if f.zeros or f.poles:
            out = _iir_pz_trans(f, w, ws.sint)
    elif kind == _FIR:
        if f.coefficients:
            out = _fir_sym_trans(f, w, ws.sint) if f.symmetry in ("B", "C") else _fir_asym_trans(f, w, ws.sint)
    elif kind == _LIST:
        amp = f.amplitudes[index]
        ph = f.phases[index] / 180.0 * math.pi
        out = complex(amp * math.cos(ph), amp * math.sin(ph))
    elif kind == _POLY:
        out = _polynomial(f, opts.b62_x)
    elif kind == _IIR:
        out = _iir_trans(f, w, ws.sint)

    # Decimation: the delay correction applies to FIR stages (asymmetric FIRs only)
    if ws.deci is not None and ws.nc != 0 and kind == _FIR:
        delay = 0.0
        if f.symmetry == "A" and f.coefficients:
            if opts.use_estimated_delay:
                delay = ws.deci.estimated_delay
            else:
                calc_delay = ((ws.nc - 1) / 2.0) * ws.deci.sample_interval
                delay = ws.deci.correction_applied - calc_delay
        shift = _time_shift(delay, w)
        out = shift if out is None else out * shift
    return out


# --------------------------------------------------------------------------- normalization


def _warn_above_nyquist(work: _Work, st: _WStage, freq: float, ident: str) -> None:
    if st.deci is not None and st.sint > 0.0 and freq > 0.5 / st.sint:
        nyquist = 0.5 / st.sint
        _warn(
            work,
            f"{ident}: Stage {st.seq} is normalized at {freq:g} Hz, above its Nyquist frequency of {nyquist:g} Hz",
        )


def _normalize(work: _Work, opts: _Options, ident: str) -> None:
    """Port of evalresp ``normalize_response`` (stage gain/normalization consistency)."""
    stages = work.stages
    nstages = len(stages)

    # TEST 1: a single-stage response must have a gain; with a stage 0 present
    # and no gain on the first stage, build one from the sensitivity.
    if nstages == 1:
        st = stages[0]
        if st.gain is None and st.kind != _POLY:
            raise ResponseValueError(f"{ident}: no stage gain defined, zero sensitivity")
    elif nstages == 2:
        st = stages[0]
        if st.gain is None and st.kind != _POLY:
            if work.sensit == 0.0:
                raise ResponseValueError(f"{ident}: no stage gain defined, zero sensitivity")
            st.gain = Gain(work.sensit, work.sensfreq)

    # TEST 2: zero gains are illegal
    for st in stages:
        if st.gain is not None and st.gain.value == 0.0:
            raise ResponseValueError(f"{ident}: zero stage gain")

    # Without a reported sensitivity use the last non-zero gain frequency
    if work.sensit == 0.0:
        for st in stages:
            if st.gain is not None and st.gain.frequency != 0.0:
                work.sensfreq = st.gain.frequency

    work.calc_sensit = 1.0
    f = work.sensfreq
    w = 2 * math.pi * f
    skipped = False

    for st in stages:
        if not opts.selects(st.seq):
            if st.seq:
                skipped = True
            continue
        if st.gain is None or not st.seq:
            continue

        main_is_pz = st.kind in (_PZ, _IIR_PZ)
        gain = st.gain
        differs = gain.frequency != work.sensfreq or (
            main_is_pz and st.filter.a0_freq != work.sensfreq
        )
        if differs:
            reset = True
            flt = st.filter
            if st.kind == _PZ:
                flt.a0 = 1.0
                df = _analog_trans(flt, gain.frequency)
                if _is_infinite(df):
                    raise ResponseValueError(
                        f"{ident}: gain frequency is at a pole of the analog filter (stage {st.seq})"
                    )
                if df == 0:
                    raise ResponseValueError(
                        f"{ident}: gain frequency of zero found in bandpass analog filter (stage {st.seq})"
                    )
                of = _analog_trans(flt, f)
                if _is_infinite(of):
                    raise ResponseValueError(
                        f"{ident}: channel sensitivity frequency is at a pole of the analog filter (stage {st.seq})"
                    )
                if of == 0:
                    raise ResponseValueError(
                        f"{ident}: channel sensitivity frequency found with bandpass analog filter (stage {st.seq})"
                    )
            elif st.kind == _IIR_PZ:
                _warn_above_nyquist(work, st, gain.frequency, ident)
                flt.a0 = 1.0
                df = _iir_pz_trans(flt, 2 * math.pi * gain.frequency, st.sint)
                of = _iir_pz_trans(flt, w, st.sint)
            elif st.kind == _FIR and flt.coefficients:
                _warn_above_nyquist(work, st, gain.frequency, ident)
                flt.h0 = 1.0
                trans = _fir_sym_trans if flt.symmetry in ("B", "C") else _fir_asym_trans
                df = trans(flt, 2 * math.pi * gain.frequency, st.sint)
                of = trans(flt, w, st.sint)
            elif st.kind == _IIR:
                _warn_above_nyquist(work, st, gain.frequency, ident)
                flt.h0 = 1.0
                df = _iir_trans(flt, 2 * math.pi * gain.frequency, st.sint)
                of = _iir_trans(flt, w, st.sint)
            else:
                reset = False

            if reset:
                gain.value = _div(gain.value, abs(df))
                gain.value *= abs(of)
                gain.frequency = f
                new_norm = _div(1.0, abs(of))
                if st.kind in (_PZ, _IIR_PZ):
                    flt.a0 = new_norm
                    flt.a0_freq = f
                elif st.kind in (_FIR, _IIR):
                    flt.h0 = new_norm

        work.calc_sensit *= gain.value
        if nstages == 1:
            work.sensit = work.calc_sensit

    # TEST 3: warn if the product of stage gains disagrees with the reported sensitivity
    if not skipped and work.sensit != 0.0:
        diff = abs((work.sensit - work.calc_sensit) / work.sensit)
        if diff >= 0.05:
            _warn(
                work,
                f"{ident}: computed ({work.calc_sensit:.5E}) and reported ({work.sensit:.5E}) "
                "sensitivities differ by more than 5 percent",
            )


# --------------------------------------------------------------------------- conversion


def _convert_to_units(inp: int, out: int, value: complex, w: float) -> complex:
    """Convert the response to the requested motion unit (evalresp ``convert_to_units``)."""
    if out == U.DEFAULT or inp not in (U.DIS, U.VEL, U.ACC):
        return value
    if inp == U.DIS:
        if out == U.DIS:
            return value
        value = value * complex(0.0, -1.0 / w) if w != 0.0 else complex(0.0, 0.0)
    elif inp == U.ACC:
        if out == U.ACC:
            return value
        value = value * complex(0.0, w)

    if out == U.DIS:
        value = value * complex(0.0, w)
    elif out == U.ACC:
        value = value * complex(0.0, -1.0 / w) if w != 0.0 else complex(0.0, 0.0)
    return value


# --------------------------------------------------------------------------- public API


# --------------------------------------------------------------------------- response-list interpolation


def _cubic_spline(t: Sequence[float], y: Sequence[float], xs: Sequence[float]) -> List[float]:
    """Natural cubic spline (zero end second derivatives) through ``(t, y)``, evaluated at ``xs``."""
    n = len(t)
    h = [t[i + 1] - t[i] for i in range(n - 1)]
    ypp = [0.0] * n
    if n > 2:
        # Interior rows i = 1..n-2 as a tridiagonal system (Thomas algorithm), index k = i - 1
        m = n - 2
        lower = [h[k] / 6.0 for k in range(m)]
        diag = [(t[k + 2] - t[k]) / 3.0 for k in range(m)]
        upper = [h[k + 1] / 6.0 for k in range(m)]
        rhs = [(y[k + 2] - y[k + 1]) / h[k + 1] - (y[k + 1] - y[k]) / h[k] for k in range(m)]
        for k in range(1, m):
            w = lower[k] / diag[k - 1]
            diag[k] -= w * upper[k - 1]
            rhs[k] -= w * rhs[k - 1]
        ypp[m] = rhs[m - 1] / diag[m - 1]
        for k in range(m - 2, -1, -1):
            ypp[k + 1] = (rhs[k] - upper[k] * ypp[k + 2]) / diag[k]

    out: List[float] = []
    for x in xs:
        i = min(max(bisect.bisect_right(t, x) - 1, 0), n - 2)
        dt = x - t[i]
        out.append(
            y[i]
            + dt
            * (
                (y[i + 1] - y[i]) / h[i]
                - (ypp[i + 1] / 6.0 + ypp[i] / 3.0) * h[i]
                + dt * (0.5 * ypp[i] + dt * (ypp[i + 1] - ypp[i]) / (6.0 * h[i]))
            )
        )
    return out


def _list_range(rl: ResponseList, requested: Sequence[float], work: _Work, ident: str) -> List[float]:
    """The requested frequencies that lie within a response list's range."""
    freqs = rl.frequencies
    n = len(freqs)
    if n < 2:
        raise ResponseValueError(f"{ident}: response list interpolation needs at least 2 frequencies")
    if any(b <= a for a, b in zip(freqs, freqs[1:])):
        raise ResponseValueError(f"{ident}: response list frequencies must be strictly increasing to interpolate")

    # values within rounding error of a limit are inside the range
    lo = freqs[0] - _RANGE_TOL * abs(freqs[0])
    hi = freqs[-1] + _RANGE_TOL * abs(freqs[-1])
    used = [f for f in requested if lo <= f <= hi]
    below = sum(1 for f in requested if f < lo)
    above = sum(1 for f in requested if f > hi)
    if not used:
        raise ResponseValueError(f"{ident}: no blockette 55 values within requested frequency range")
    if below:
        _warn(work, f"{ident}: dropped {below} values from start of requested range, outside blockette 55")
    if above:
        _warn(work, f"{ident}: dropped {above} values from end of requested range, outside blockette 55")
    return used


def _same_frequencies(a: Sequence[float], b: Sequence[float]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= 1e-6 * abs(y) for x, y in zip(a, b))


def _interpolate_list(rl: ResponseList, used: Sequence[float], work: _Work, ident: str) -> ResponseList:
    """Resample a response list at frequencies within its range.

    Amplitudes and unwrapped phases are splined separately against log10(frequency), the
    amplitudes as log10(amplitude), and phases are returned wrapped.  If any listed
    frequency or amplitude, or any requested frequency, is not positive, the splines run
    in linear space instead and non-positive amplitudes are replaced by a tenth of the
    smallest listed amplitude.
    """
    freqs = rl.frequencies
    use_log = all(f > 0.0 for f in freqs) and all(a > 0.0 for a in rl.amplitudes) and all(f > 0.0 for f in used)
    if use_log:
        knots = [math.log10(f) for f in freqs]
        points = [math.log10(f) for f in used]
        amps = [10.0**v for v in _cubic_spline(knots, [math.log10(a) for a in rl.amplitudes], points)]
    else:
        _warn(
            work,
            f"{ident}: response list has non-positive frequency or amplitude values; "
            "interpolating in linear space (results may be inaccurate)",
        )
        knots, points = list(freqs), used
        amps = _cubic_spline(knots, rl.amplitudes, points)
        floor = min(rl.amplitudes)
        if floor > 0.0:
            amps = [a if a > 0.0 else floor / 10.0 for a in amps]

    unwrapped: List[float] = []
    prev = added = 0.0
    any_unwrapped = False
    for ph in rl.phases:
        new, added = unwrap_phase(ph, prev, 360.0, added)
        unwrapped.append(new)
        any_unwrapped = any_unwrapped or added != 0.0
        prev = new
    phases = _cubic_spline(knots, unwrapped, points)

    if any_unwrapped:
        offset = 0.0
        while phases[0] + offset > 180.0:
            offset -= 360.0
        while phases[0] + offset < -180.0:
            offset += 360.0
        added = offset
        for i, ph in enumerate(phases):
            ph += added
            if ph > 180.0:
                added -= 360.0
                ph -= 360.0
            elif ph < -180.0:
                added += 360.0
                ph += 360.0
            phases[i] = ph

    return ResponseList(list(used), amps, phases)


def evaluate(
    channel: Channel,
    frequencies: Sequence[float],
    *,
    unit: str = "def",
    stages: Optional[Tuple[Optional[int], Optional[int]]] = None,
    use_total_sensitivity: bool = False,
    use_estimated_delay: bool = False,
    b62_x: float = 0.0,
    per_stage: bool = False,
    interpolate_list: bool = False,
    exclude_stages: Optional[Iterable[int]] = None,
) -> ResponseResult:
    """Evaluate a channel's complex response at the given frequencies (Hz).

    Parameters
    ----------
    channel:
        The channel (epoch) to evaluate.
    frequencies:
        Frequencies in Hz.  If a selected stage is a response list the listed
        frequencies are used instead (as in evalresp); several lists must share
        their frequencies, or be interpolated.
    unit:
        ``"def"`` (default) for the unit documented in the response, or ``"vel"``,
        ``"dis"``, ``"acc"`` (evalresp's own default is ``"vel"``).
    stages:
        ``(first, last)`` inclusive stage numbers; either may be ``None``.
    use_total_sensitivity:
        Use the reported overall sensitivity (stage 0) rather than the product
        of stage gains.
    use_estimated_delay:
        For asymmetric FIR filters use the estimated delay instead of the
        correction applied.
    b62_x:
        Input value at which a polynomial (blockette 62) response is evaluated; any
        value is accepted, including the default 0.
    per_stage:
        Also return each selected stage's contribution in ``stage_values``.
        The product of the per-stage values equals the total response; unit
        scaling/conversion factors are included in the first selected stage.
    exclude_stages:
        Stage numbers to leave out of the calculation (in addition to any
        ``stages`` range).
    interpolate_list:
        Spline-interpolate each selected response-list (blockette 55) stage at the
        requested frequencies (those outside any list's range are dropped) instead of
        reporting the listed frequencies.
    """
    opts = _Options(unit, stages, use_total_sensitivity, use_estimated_delay, b62_x, exclude_stages)
    ident = channel.nslc
    # evalresp reports a zero amplitude (with a warning) here; an error is clearer than a flat curve
    if channel.stages and all(s.sequence == 0 and s.filter is None for s in channel.stages):
        raise ResponseValueError(f"{ident}: response has only an overall sensitivity and no filter stages")
    work = _prepare(channel, opts)
    _normalize(work, opts, ident)

    selected = [s for s in work.stages if opts.selects(s.seq)]
    if not selected:
        raise ResponseValueError(f"{ident}: no matching stages found for requested stage range")

    freqs = list(frequencies)
    lists = [s for s in selected if s.kind == _LIST]
    if lists and interpolate_list:
        for s in lists:
            freqs = _list_range(s.filter, freqs, work, ident)
        sampled = {s.seq: _interpolate_list(s.filter, freqs, work, ident) for s in lists}
        selected = [replace(s, filter=sampled[s.seq]) if s.seq in sampled else s for s in selected]
    elif lists:
        first = lists[0]
        for s in lists[1:]:
            if not _same_frequencies(s.filter.frequencies, first.filter.frequencies):
                raise ResponseValueError(
                    f"{ident}: response list stages {first.seq} and {s.seq} have different frequencies, "
                    "so they can only be combined by interpolating"
                )
        freqs = list(first.filter.frequencies)
    if not freqs:
        raise ResponseValueError("No frequencies requested")

    first_units_class = work.first_units_class
    total_sens = work.sensit if use_total_sensitivity else work.calc_sensit

    values: List[complex] = []
    stage_values: Optional[Dict[int, List[complex]]] = {s.seq: [] for s in selected if s.seq} if per_stage else None
    first_contrib = next((s for s in selected if s.seq), None)

    for idx, fr in enumerate(freqs):
        w = 2 * math.pi * fr
        val = complex(1.0, 0.0)
        infinite = False
        for ws in selected:
            r = _filter_response(ws, fr, idx, opts, ident)
            if r is not None:
                if ws.kind == _PZ and _is_infinite(r):
                    infinite = True
                    _warn(
                        work,
                        f"{ident}: Stage {ws.seq} has a pole at {fr:g} Hz with no matching zero; "
                        "the response is infinite there",
                    )
                val = val * r
            if stage_values is not None and ws.seq:
                contrib = r if r is not None else complex(1.0, 0.0)
                if ws.gain is not None:
                    contrib = contrib * ws.gain.value
                if ws is first_contrib and total_sens != work.calc_sensit and work.calc_sensit:
                    contrib = contrib * (total_sens / work.calc_sensit)  # keep product == total
                if ws is first_contrib:
                    contrib = _convert_to_units(first_units_class, opts.unit, contrib * work.unit_scale, w)
                stage_values[ws.seq].append(contrib)
        if infinite:
            val = complex(math.inf, 0.0)
        out = complex(val.real * total_sens * work.unit_scale, val.imag * total_sens * work.unit_scale)
        out = _convert_to_units(first_units_class, opts.unit, out, w)
        values.append(out)

    return ResponseResult(
        channel=channel,
        frequencies=freqs,
        values=values,
        unit=opts.unit_key,
        info=_build_info(work, opts, total_sens),
        stage_values=stage_values,
    )


def _build_info(work: _Work, opts: _Options, total_sens: float) -> ResponseInfo:
    infos: List[StageInfo] = []
    first_in = last_out = None
    for ws in work.stages:
        selected = opts.selects(ws.seq)
        if selected and ws.in_units and first_in is None:
            first_in = ws.in_units
        if selected and ws.out_units:
            last_out = ws.out_units
        a0 = None
        desc = ""
        f = ws.filter
        if isinstance(f, PolesZeros):
            a0 = f.a0
            desc = f"{len(f.zeros)} zeros, {len(f.poles)} poles"
        elif isinstance(f, FIR):
            a0 = f.h0
            desc = f"{_fir_ncoeffs(f)} coefficients"
        elif isinstance(f, IIRCoefficients):
            a0 = f.h0
            desc = f"{len(f.numerators)} numerators, {len(f.denominators)} denominators"
        elif isinstance(f, ResponseList):
            desc = f"{len(f.frequencies)} frequencies"
        elif isinstance(f, Polynomial):
            desc = f"{len(f.coefficients)} coefficients"
        infos.append(
            StageInfo(
                summary=_stage_summary(ws),
                sequence=ws.seq,
                kind=ws.kind,
                gain=ws.gain.value if ws.gain else None,
                gain_frequency=ws.gain.frequency if ws.gain else None,
                a0=a0,
                description=desc,
                sample_interval=ws.sint if ws.deci else None,
                selected=selected,
            )
        )
    return ResponseInfo(
        input_units=first_in,
        output_units=last_out,
        requested_unit=U.motion_unit_name(opts.unit),
        reported_sensitivity=work.sensit,
        calculated_sensitivity=work.calc_sensit,
        sensitivity_frequency=work.sensfreq,
        calculated_delay=work.calc_delay,
        estimated_delay=work.estim_delay,
        correction_applied=work.applied_corr,
        sample_interval=work.sint,
        stages=infos,
        warnings=list(work.warnings),
    )


def _stage_summary(ws: _WStage) -> str:
    """One line per stage, formatted like evalresp's verbose (``-v``) output."""
    if not ws.seq:
        return ""
    parts: List[str] = []
    f = ws.filter
    first = True
    if isinstance(f, Polynomial):
        parts.append(f" POLYNOMIAL     MacLaurin Coeffs= {len(f.coefficients):2d}")
    elif isinstance(f, PolesZeros):
        label = {LAPLACE_RADIANS: "LAPLACE    ", LAPLACE_HERTZ: "ANALOG     ", Z_TRANSFORM: "IIR_PZ     "}[f.kind]
        parts.append(f" {label} A0={f.a0:E} NZeros= {len(f.zeros):2d} NPoles= {len(f.poles):2d}")
    elif isinstance(f, FIR):
        label = {"A": "FIR_ASYM  ", "B": "FIR_SYM_1 ", "C": "FIR_SYM_2 "}[f.symmetry]
        parts.append(f" {label} H0={f.h0:E} Ncoeff={_fir_ncoeffs(f):3d}")
    elif isinstance(f, IIRCoefficients):
        parts.append(f"IIR_COEFFS   H0={f.h0:E} NNumers={len(f.numerators):2d} NDenums= {len(f.denominators):2d}")
    elif isinstance(f, Generic):
        parts.append(" Generic blockette is ignored; ")
    if f is not None:
        first = False
    if ws.deci is not None:
        parts.append(f" SamInt={ws.deci.sample_interval:E}")
        first = False
    if ws.gain is not None:
        parts.append((" GAIN        Sd=" if first else " Sd=") + f"{ws.gain.value:E}")
    return "".join(parts)

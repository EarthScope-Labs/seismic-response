"""Data model for an instrument response.

The model deliberately mirrors the structure that evalresp builds after it has
parsed a RESP file (StationXML is converted to RESP-equivalent content by
evalresp before parsing, so a single model serves both input formats).

A :class:`Channel` holds an ordered list of :class:`Stage` objects.  Each stage
has at most one *filter* (poles/zeros, FIR, IIR coefficients, response list,
polynomial or generic), an optional :class:`Decimation` and an optional
:class:`Gain`.  Stage number 0 is the overall instrument sensitivity (a stage
holding only a gain) as in SEED/RESP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Union

__all__ = [
    "PolesZeros",
    "FIR",
    "IIRCoefficients",
    "ResponseList",
    "Polynomial",
    "Generic",
    "Decimation",
    "Gain",
    "Stage",
    "Channel",
    "Filter",
    "nslc_to_source_id",
    "source_id_to_nslc",
]


# Transfer function kinds for poles/zeros
LAPLACE_RADIANS = "laplace_rad"  # RESP 'A'
LAPLACE_HERTZ = "laplace_hz"  # RESP 'B'
Z_TRANSFORM = "z"  # RESP 'D'


@dataclass
class PolesZeros:
    """Poles and zeros (RESP blockette 53, StationXML ``PolesZeros``)."""

    kind: str  # LAPLACE_RADIANS, LAPLACE_HERTZ or Z_TRANSFORM
    zeros: List[complex]
    poles: List[complex]
    a0: float = 1.0  # normalization factor
    a0_freq: float = 0.0  # frequency (Hz) of the normalization factor


@dataclass
class FIR:
    """FIR coefficients (RESP blockettes 54 with no denominators, and 61).

    ``symmetry`` is the SEED symmetry code: ``'A'`` no symmetry (all
    coefficients given), ``'B'`` odd number of coefficients with symmetry (the
    second half is omitted) and ``'C'`` even number of coefficients with
    symmetry (the second half is omitted).
    """

    symmetry: str
    coefficients: List[float]
    h0: float = 1.0


@dataclass
class IIRCoefficients:
    """Digital filter with numerator and denominator coefficients (blockette 54)."""

    numerators: List[float]
    denominators: List[float]
    h0: float = 1.0


@dataclass
class ResponseList:
    """Explicit frequency/amplitude/phase list (blockette 55)."""

    frequencies: List[float]
    amplitudes: List[float]
    phases: List[float]  # degrees


@dataclass
class Polynomial:
    """MacLaurin polynomial (blockette 62), for non-linear sensors."""

    coefficients: List[float]
    approximation_type: str = "M"
    frequency_units: str = "B"
    lower_freq_bound: float = 0.0
    upper_freq_bound: float = 0.0
    lower_approx_bound: float = 0.0
    upper_approx_bound: float = 0.0
    max_abs_error: float = 0.0


@dataclass
class Generic:
    """Generic response (blockette 56); recognized but ignored, as in evalresp."""


Filter = Union[PolesZeros, FIR, IIRCoefficients, ResponseList, Polynomial, Generic]


@dataclass
class Decimation:
    """Decimation information (blockette 57)."""

    input_sample_rate: float = 0.0
    factor: int = 1
    offset: int = 0
    estimated_delay: float = 0.0  # seconds
    correction_applied: float = 0.0  # seconds

    @property
    def sample_interval(self) -> float:
        """Input sample interval in seconds (0 when the rate is 0, as evalresp)."""
        return 1.0 / self.input_sample_rate if self.input_sample_rate else 0.0


@dataclass
class Gain:
    """Gain (blockette 58): stage gain, or total sensitivity for stage 0."""

    value: float
    frequency: float = 0.0


@dataclass
class Stage:
    """One stage of a response."""

    sequence: int
    input_units: Optional[str] = None  # e.g. "M/S - Velocity in Meters Per Second"
    output_units: Optional[str] = None
    filter: Optional[Filter] = None
    decimation: Optional[Decimation] = None
    gain: Optional[Gain] = None

    @property
    def filter_kind(self) -> str:
        """Short name of the filter type, for display."""
        f = self.filter
        if f is None:
            return "gain"
        if isinstance(f, PolesZeros):
            return {
                LAPLACE_RADIANS: "poles-zeros (Laplace, rad/s)",
                LAPLACE_HERTZ: "poles-zeros (Laplace, Hz)",
                Z_TRANSFORM: "poles-zeros (Z-transform)",
            }[f.kind]
        if isinstance(f, FIR):
            return {"A": "FIR (asymmetric)", "B": "FIR (symmetric, odd)", "C": "FIR (symmetric, even)"}[
                f.symmetry
            ]
        if isinstance(f, IIRCoefficients):
            return "IIR coefficients"
        if isinstance(f, ResponseList):
            return "response list"
        if isinstance(f, Polynomial):
            return "polynomial"
        return "generic"


@dataclass
class Channel:
    """A channel epoch with its full response."""

    network: str
    station: str
    location: str
    channel: str
    start_time: Optional[datetime] = None  # timezone-aware (UTC)
    end_time: Optional[datetime] = None  # None means open-ended
    stages: List[Stage] = field(default_factory=list)
    origin: str = ""  # e.g. file name the channel was read from
    instrument: str = ""  # instrument description, when the input carries one

    @property
    def source_id(self) -> str:
        """FDSN Source Identifier, e.g. ``FDSN:IU_ANMO_00_B_H_Z``."""
        return nslc_to_source_id(self.network, self.station, self.location, self.channel)

    @property
    def nslc(self) -> str:
        """Dotted SEED identifier, e.g. ``IU.ANMO.00.BHZ``."""
        return f"{self.network}.{self.station}.{self.location}.{self.channel}"

    @property
    def sensitivity(self) -> Optional[Gain]:
        """Overall sensitivity (stage 0 gain) if the response has one."""
        for st in self.stages:
            if st.sequence == 0 and st.gain is not None:
                return st.gain
        return None

    def stage(self, sequence: int) -> Optional[Stage]:
        for st in self.stages:
            if st.sequence == sequence:
                return st
        return None

    def contains(self, time: datetime) -> bool:
        """True if ``time`` falls within [start_time, end_time)."""
        if self.start_time is not None and self.start_time > time:
            return False
        if self.end_time is not None and self.end_time <= time:
            return False
        return True

    def evaluate(self, *args, **kwargs):
        """Evaluate this channel's response; see :func:`seismic_response.evaluate`."""
        from .evaluate import evaluate

        return evaluate(self, *args, **kwargs)

    def __str__(self) -> str:
        n = sum(1 for s in self.stages if s.sequence != 0)
        return (
            f"{self.source_id} {_fmt_time(self.start_time)} - {_fmt_time(self.end_time, 'open')} "
            f"({n} stages)"
        )


def _fmt_time(t: Optional[datetime], none: str = "?") -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t is not None else none


def nslc_to_source_id(network: str = "", station: str = "", location: str = "", channel: str = "") -> str:
    """Convert SEED network/station/location/channel codes to an FDSN Source ID.

    A three character channel code is expanded to band_source_subsource form.

    >>> nslc_to_source_id("XX", "TEST", "", "LHZ")
    'FDSN:XX_TEST__L_H_Z'
    """
    if len(channel) == 3:
        channel = f"{channel[0]}_{channel[1]}_{channel[2]}"
    return f"FDSN:{network}_{station}_{location}_{channel}"


def source_id_to_nslc(source_id: str):
    """Split an FDSN Source ID into (network, station, location, channel).

    >>> source_id_to_nslc("FDSN:IU_ANMO_00_B_H_Z")
    ('IU', 'ANMO', '00', 'BHZ')
    """
    sid = source_id[5:] if source_id.upper().startswith("FDSN:") else source_id
    parts = sid.split("_")
    if len(parts) < 4:
        raise ValueError(f"Not a channel level source identifier: {source_id!r}")
    net, sta, loc = parts[0], parts[1], parts[2]
    chan = "".join(parts[3:])
    return net, sta, loc, chan

"""Text output in the formats written by evalresp.

* ``fap``: ``frequency amplitude phase`` (phase in degrees, always unwrapped)
* ``ap``:  separate amplitude and phase listings (``AMP.*`` and ``PHASE.*`` files)
* ``cs``:  complex spectra, ``frequency real imaginary``
"""

from __future__ import annotations

from typing import List, Tuple

from .evaluate import ResponseResult

__all__ = [
    "format_fap",
    "format_amplitude",
    "format_phase",
    "format_complex",
    "output_filename",
    "render",
    "describe",
    "OUTPUT_FORMATS",
]

OUTPUT_FORMATS = ("ap", "fap", "cs")


def format_fap(result: ResponseResult, unwrap: bool = True) -> str:
    """Frequency, amplitude, phase lines (``%.6E %.6E %.6E``)."""
    amps = result.amplitudes
    phases = result.phases(unwrap=unwrap)
    return "".join(
        f"{f:.6E} {a:.6E} {p:.6E}\n" for f, a, p in zip(result.frequencies, amps, phases)
    )


def format_amplitude(result: ResponseResult) -> str:
    return "".join(f"{f:.6E} {a:.6E}\n" for f, a in zip(result.frequencies, result.amplitudes))


def format_phase(result: ResponseResult, unwrap: bool = False) -> str:
    return "".join(
        f"{f:.6E} {p:.6E}\n" for f, p in zip(result.frequencies, result.phases(unwrap=unwrap))
    )


def format_complex(result: ResponseResult) -> str:
    """Complex spectra: frequency, real, imaginary (two space separators, as evalresp)."""
    return "".join(
        f"{f:.6E}  {v.real:.6E}  {v.imag:.6E}\n" for f, v in zip(result.frequencies, result.values)
    )


_PREFIX = {"fap": "FAP", "amp": "AMP", "phase": "PHASE", "cs": "SPECTRA"}


def output_filename(result: ResponseResult, kind: str, stdio: bool = False) -> str:
    """Name evalresp gives an output file, e.g. ``AMP.IU.ANMO.00.BHZ``.

    With ``stdio`` the combined FAP output is labelled ``AMP/PHS`` as in evalresp.
    """
    prefix = "AMP/PHS" if (stdio and kind == "fap") else _PREFIX[kind]
    c = result.channel
    return f"{prefix}.{c.network}.{c.station}.{c.location}.{c.channel}"


def render(
    result: ResponseResult, fmt: str = "ap", unwrap: bool = False, stdio: bool = False
) -> List[Tuple[str, str]]:
    """Render a response in an evalresp output format.

    Returns ``[(filename, text), ...]``: one entry for ``fap`` and ``cs``, two
    (amplitude, then phase) for ``ap``.  FAP phase is always unwrapped, as in
    evalresp; AP phase is unwrapped only when ``unwrap`` is true.
    """
    fmt = fmt.lower()
    if fmt == "fap":
        return [(output_filename(result, "fap", stdio), format_fap(result, unwrap=True))]
    if fmt == "ap":
        return [
            (output_filename(result, "amp", stdio), format_amplitude(result)),
            (output_filename(result, "phase", stdio), format_phase(result, unwrap=unwrap)),
        ]
    if fmt == "cs":
        return [(output_filename(result, "cs", stdio), format_complex(result))]
    raise ValueError(f"Unknown output format {fmt!r}; expected one of {OUTPUT_FORMATS}")


def describe(result: ResponseResult, source: str = "") -> List[str]:
    """Verbose description of a computed response, like evalresp's ``-v`` output."""
    c = result.channel
    i = result.info
    sep = " " + "-" * 50
    start = _resp_time(c.start_time)
    end = _resp_time(c.end_time) if c.end_time else "No Ending Time"
    lines = [
        sep,
        f"  {source or c.origin or 'RESP.' + c.network + '.' + c.station + '.' + c.location + '.' + c.channel}",
        sep,
        f"  {c.network or '??'} {c.station} {c.location or '??'} {c.channel}",
        f"  {start} {end}",
        f"   documented input units: {i.input_units or 'None'}",
        f"   documented output units: {i.output_units or 'None'}",
        f"   requested units: {i.requested_unit}",
        f"   computed sens={i.calculated_sensitivity:.5E} (reported={i.reported_sensitivity:.5E}) "
        f"@ {i.sensitivity_frequency:.5E} Hz",
        f"   calc_del={i.calculated_delay:.5E}  corr_app={i.correction_applied:.5E}  "
        f"est_delay={i.estimated_delay:.5E}  final_sint={i.sample_interval:.3g}(sec/sample)",
    ]
    for s in i.stages:
        if s.selected and s.sequence and s.summary:
            lines.append(f"     stage {s.sequence:2d}:{s.summary}")
    lines.append("-" * 50)
    return lines


def _resp_time(t) -> str:
    if t is None:
        return ""
    return f"{t.year},{t.timetuple().tm_yday:03d},{t.hour:02d}:{t.minute:02d}:{t.second:02d}"

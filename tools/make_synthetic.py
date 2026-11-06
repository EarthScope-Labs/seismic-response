#!/usr/bin/env python3
"""Generate small synthetic RESP files that exercise response types and code paths.

The real-world evalresp test data does not cover every filter type (for example
Hz poles/zeros, Z-transform poles/zeros, IIR coefficients, odd-symmetric FIR or
unit conversion from displacement/acceleration inputs).  These synthetic files
fill the gaps; the C evalresp program is then run on them to produce the
reference results that the test-suite compares against.

    python tools/make_synthetic.py tests/data/synthetic
"""

import sys
from pathlib import Path

HEADER = """#
B050F03     Station:     {sta}
B050F16     Network:     SY
B052F03     Location:    {loc}
B052F04     Channel:     {cha}
B052F22     Start date:  2000,001,00:00:00
B052F23     End date:    No Ending Time
"""

VEL = "M/S - Velocity in Meters Per Second"
ACC = "M/S**2 - Acceleration in Meters Per Second Per Second"
DIS = "M - Displacement in Meters"
NMS2 = "NM/S**2 - Acceleration in Nanometers Per Second Per Second"
VOLTS = "V - Volts"
COUNTS = "COUNTS - Digital Counts"


def e(x):
    return f"{x:+.6E}"


def pz(stage, kind, inp, out, a0, a0f, zeros, poles):
    code = {"A": "A [Laplace Transform (Rad/sec)]", "B": "B [Analog (Hz)]", "D": "D [Digital (Z-transform)]"}[kind]
    lines = [
        f"B053F03     Transfer function type:                {code}",
        f"B053F04     Stage sequence number:                 {stage}",
        f"B053F05     Response in units lookup:              {inp}",
        f"B053F06     Response out units lookup:             {out}",
        f"B053F07     A0 normalization factor:               {a0!r}",
        f"B053F08     Normalization frequency:               {a0f!r}",
        f"B053F09     Number of zeroes:                      {len(zeros)}",
        f"B053F14     Number of poles:                       {len(poles)}",
    ]
    for i, z in enumerate(zeros):
        lines.append(f"B053F10-13  {i:3d}  {e(z.real)}  {e(z.imag)}  {e(0)}  {e(0)}")
    for i, p in enumerate(poles):
        lines.append(f"B053F15-18  {i:3d}  {e(p.real)}  {e(p.imag)}  {e(0)}  {e(0)}")
    return "\n".join(lines) + "\n"


def gain(stage, g, f):
    return (
        f"B058F03     Stage sequence number:                 {stage}\n"
        f"B058F04     Gain:                                  {g!r}\n"
        f"B058F05     Frequency of gain:                     {f!r} HZ\n"
        f"B058F06     Number of calibrations:                0\n"
    )


def deci(stage, rate, factor, delay=0.0, corr=0.0):
    return (
        f"B057F03     Stage sequence number:                 {stage}\n"
        f"B057F04     Input sample rate:                     {rate!r}\n"
        f"B057F05     Decimation factor:                     {factor}\n"
        f"B057F06     Decimation offset:                     0\n"
        f"B057F07     Estimated delay (seconds):             {delay!r}\n"
        f"B057F08     Correction applied (seconds):          {corr!r}\n"
    )


def fir_b61(stage, symmetry, coeffs, inp=COUNTS, out=COUNTS):
    lines = [
        f"B061F03     Stage sequence number:                 {stage}",
        "B061F04     Response Name:                         SYNTH",
        f"B061F05     Symmetry Code:                         {symmetry}",
        f"B061F06     Response in units lookup:              {inp}",
        f"B061F07     Response out units lookup:             {out}",
        f"B061F08     Number of Coefficients:                {len(coeffs)}",
    ]
    for i, c in enumerate(coeffs):
        lines.append(f"B061F09     {i:3d}  {e(c)}")
    return "\n".join(lines) + "\n"


def coeff_b54(stage, inp, out, numers, denoms=()):
    """Coefficients blockette in the order evalresp's C parser needs for IIR filters.

    The counts (F07, F10) come first and the coefficient lines after, as written
    by evalresp's own StationXML converter.  (The C parser skips forward looking
    for F10 before reading the numerators, so SEED order cannot be parsed by C;
    this package accepts either order.)
    """
    lines = [
        "B054F03     Transfer function type:                D",
        f"B054F04     Stage sequence number:                 {stage}",
        f"B054F05     Response in units lookup:              {inp}",
        f"B054F06     Response out units lookup:             {out}",
        f"B054F07     Number of numerators:                  {len(numers)}",
        f"B054F10     Number of denominators:                {len(denoms)}",
    ]
    for i, c in enumerate(numers):
        lines.append(f"B054F08-09  {i:3d}  {e(c)}  {e(0)}")
    for i, c in enumerate(denoms):
        lines.append(f"B054F11-12  {i:3d}  {e(c)}  {e(0)}")
    return "\n".join(lines) + "\n"


def sensitivity(g, f):
    return gain(0, g, f)


# A reasonable 2nd-order seismometer: poles at -0.037 +/- 0.037i rad/s (30 s) plus a zero pair at the origin
SEISMO_ZEROS = [0j, 0j]
SEISMO_POLES = [complex(-0.0370, 0.0370), complex(-0.0370, -0.0370)]


def files():
    out = {}

    # 1. Hz poles/zeros (ANALOG type) + odd-symmetric FIR (B061 code B)
    hz_zeros = [0j, 0j]
    hz_poles = [complex(-0.00589, 0.00589), complex(-0.00589, -0.00589)]
    out["pz_hz_fir_odd"] = (
        ("SYN1", "00", "BHZ"),
        pz(1, "B", VEL, VOLTS, 2.5, 1.0, hz_zeros, hz_poles)
        + gain(1, 1500.0, 1.0)
        + coeff_b54(2, VOLTS, COUNTS, [])
        + deci(2, 1000.0, 1)
        + gain(2, 400000.0, 0.0)
        + fir_b61(3, "B", [0.05, 0.12, 0.28, 0.5])
        + deci(3, 1000.0, 2, 0.0, 0.0)
        + gain(3, 1.0, 0.0)
        + sensitivity(600000000.0, 1.0),
    )

    # 2. Z-transform poles/zeros (IIR_PZ) after an analog stage
    out["iir_pz"] = (
        ("SYN2", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 1.0, 0.1, SEISMO_ZEROS, SEISMO_POLES)
        + gain(1, 1000.0, 0.1)
        + pz(2, "D", VOLTS, COUNTS, 0.8, 0.5, [complex(-1.0, 0.0)], [complex(0.5, 0.2), complex(0.5, -0.2)])
        + deci(2, 100.0, 1)
        + gain(2, 100000.0, 0.5)
        + sensitivity(1.0e8, 0.1),
    )

    # 3. IIR coefficients (B054 with denominators)
    out["iir_coeff"] = (
        ("SYN3", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 1.0, 0.1, SEISMO_ZEROS, SEISMO_POLES)
        + gain(1, 1000.0, 0.1)
        + coeff_b54(2, VOLTS, COUNTS, [0.2, 0.4, 0.2], [1.0, -0.5, 0.25])
        + deci(2, 100.0, 1)
        + gain(2, 1000.0, 0.1)
        + sensitivity(1.0e6, 0.1),
    )

    # 4. FIR continuation: one stage split across two B061 blockettes (asymmetric, then detected as symmetric)
    out["fir_continuation"] = (
        ("SYN4", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 1.0, 0.1, SEISMO_ZEROS, SEISMO_POLES)
        + gain(1, 1000.0, 0.1)
        + fir_b61(2, "A", [0.05, 0.1, 0.2, 0.15], inp=VOLTS, out=COUNTS)
        + fir_b61(2, "A", [0.15, 0.2, 0.1, 0.05], inp=VOLTS, out=COUNTS)
        + deci(2, 200.0, 2, 0.01, 0.015)
        + gain(2, 1000.0, 0.0)
        + sensitivity(1.0e6, 0.1),
    )

    # 5. Genuinely asymmetric FIR (delay correction paths: corrected vs estimated delay)
    out["fir_asym"] = (
        ("SYN5", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 1.0, 0.1, SEISMO_ZEROS, SEISMO_POLES)
        + gain(1, 1000.0, 0.1)
        + fir_b61(2, "A", [0.4, 0.3, 0.2, 0.08, 0.02], inp=VOLTS, out=COUNTS)
        + deci(2, 200.0, 2, 0.011, 0.007)
        + gain(2, 500.0, 0.0)
        + sensitivity(5.0e5, 0.1),
    )

    # 6. Gain frequency different from normalization and sensitivity frequencies (renormalization)
    out["gain_freq_mismatch"] = (
        ("SYN6", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 1.0, 0.5, SEISMO_ZEROS, SEISMO_POLES)
        + gain(1, 1000.0, 2.0)
        + fir_b61(2, "C", [0.05, 0.15, 0.3, 0.5][::-1][::-1], inp=VOLTS, out=COUNTS)
        + deci(2, 200.0, 2)
        + gain(2, 250.0, 1.0)
        + sensitivity(2.5e5, 0.05),
    )

    # 7-9. Input units: displacement, acceleration, and an SI-prefixed (nm) acceleration
    for name, sta, units, zeros in (
        ("units_dis", "SYN7", DIS, []),
        ("units_acc", "SYN8", ACC, [0j, 0j, 0j][:0]),
        ("units_nm_acc", "SYN9", NMS2, []),
    ):
        poles = SEISMO_POLES
        out[name] = (
            (sta, "00", "BHZ"),
            pz(1, "A", units, VOLTS, 2.0, 0.1, zeros, poles)
            + gain(1, 800.0, 0.1)
            + coeff_b54(2, VOLTS, COUNTS, [])
            + deci(2, 100.0, 1)
            + gain(2, 100000.0, 0.0)
            + sensitivity(8.0e7, 0.1),
        )

    # 10. Single stage response without a sensitivity (nstages == 1: calculated sensitivity is used)
    out["single_stage"] = (
        ("SYN10", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 3.0, 1.0, SEISMO_ZEROS, SEISMO_POLES) + gain(1, 1200.0, 1.0),
    )

    # 11. Stage with a gain and a sensitivity but gain-only intermediate stage
    out["gain_only_stage"] = (
        ("SYN11", "00", "BHZ"),
        pz(1, "A", VEL, VOLTS, 1.0, 0.1, SEISMO_ZEROS, SEISMO_POLES)
        + gain(1, 1000.0, 0.1)
        + gain(2, 2.0, 0.1)
        + coeff_b54(3, VOLTS, COUNTS, [])
        + deci(3, 100.0, 1)
        + gain(3, 1000.0, 0.0)
        + sensitivity(2.0e6, 0.1),
    )
    return out


def write(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, ((sta, loc, cha), body) in files().items():
        text = HEADER.format(sta=sta, loc=loc, cha=cha) + body
        (dest / f"{name}.resp").write_text(text)
        print("wrote", dest / f"{name}.resp")


if __name__ == "__main__":
    write(Path(sys.argv[1] if len(sys.argv) > 1 else "tests/data/synthetic"))

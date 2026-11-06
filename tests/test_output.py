"""Text output formats."""

import cmath
import math

import pytest
from test_evaluate import chan, lowpass

from seismic_response import output as out


@pytest.fixture
def result():
    return chan(lowpass(1.0)).evaluate([0.5, 1.0, 2.0], unit="def")


def test_fap_format(result):
    lines = out.format_fap(result).splitlines()
    assert len(lines) == 3
    f, a, p = lines[1].split()
    assert f == "1.000000E+00"
    assert float(a) == pytest.approx(1 / math.sqrt(2), rel=1e-6) and a == f"{abs(result.values[1]):.6E}"
    # FAP phase is unwrapped, and evalresp forces the *first* phase into [0, 360):
    # -26.6 deg at 0.5 Hz becomes 333.4, so -45 deg at 1 Hz is reported as 315.
    assert result.phases()[1] == pytest.approx(-45.0)
    assert float(lines[0].split()[2]) == pytest.approx(360 - math.degrees(math.atan(0.5)))
    assert float(p) == pytest.approx(315.0)
    assert lines[1] == f"{f} {a} {p}"  # single-space separated, like evalresp


def test_complex_format_uses_two_space_separators(result):
    line = out.format_complex(result).splitlines()[1]
    f, re_, im = line.split("  ")
    assert float(re_) == pytest.approx(result.values[1].real, rel=1e-6)
    assert float(im) == pytest.approx(result.values[1].imag, rel=1e-6)


def test_amplitude_and_phase_listings(result):
    assert out.format_amplitude(result).splitlines()[0].count(" ") == 1
    assert out.format_phase(result).splitlines()[2].split()[1].startswith("-")


def test_exponent_format_matches_c_printf():
    assert f"{1e100:.6E}" == "1.000000E+100"
    assert f"{1.5e-5:.6E}" == "1.500000E-05"


def test_render_filenames_and_parts(result):
    assert [n for n, _ in out.render(result, "ap")] == ["AMP.XX.TEST.00.BHZ", "PHASE.XX.TEST.00.BHZ"]
    assert [n for n, _ in out.render(result, "fap")] == ["FAP.XX.TEST.00.BHZ"]
    assert [n for n, _ in out.render(result, "cs")] == ["SPECTRA.XX.TEST.00.BHZ"]
    assert [n for n, _ in out.render(result, "fap", stdio=True)] == ["AMP/PHS.XX.TEST.00.BHZ"]
    with pytest.raises(ValueError):
        out.render(result, "xml")


def test_fap_phase_is_always_unwrapped_but_ap_phase_only_on_request(result):
    # An artificial response whose phase falls 40 degrees per frequency step (past -360)
    result.values = [cmath.rect(1.0, math.radians(-40 * i)) for i in range(12)]
    result.frequencies = [float(i + 1) for i in range(12)]
    fap_phase = [float(line.split()[2]) for line in out.format_fap(result).splitlines()]
    ap_phase = [float(line.split()[1]) for line in out.format_phase(result).splitlines()]
    assert min(fap_phase) < -360 + 40  # unwrapping carries the phase on past -360
    assert min(ap_phase) > -180.0001  # AP output stays wrapped to +-180...
    unwrapped_ap = [float(line.split()[1]) for line in out.format_phase(result, unwrap=True).splitlines()]
    assert unwrapped_ap == pytest.approx(fap_phase)  # ...unless -unwrap is given


def test_describe_mentions_key_facts(anmo):
    res = anmo[0].evaluate([1.0], unit="vel")
    text = "\n".join(out.describe(res))
    assert "IU ANMO 00 BHZ" in text
    assert "requested units: Velocity (m/s)" in text
    assert "computed sens=8.64679E+08 (reported=8.64730E+08) @ 2.00000E-02 Hz" in text
    assert "stage  1: LAPLACE" in text and "A0=8.608300E+04 NZeros=  2 NPoles=  5 Sd=2.061680E+03" in text
    assert "FIR_SYM_2" in text

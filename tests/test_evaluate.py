"""Evaluation: analytic checks, normalization, unit conversion, errors and options."""

import math
from datetime import datetime, timezone

import pytest

import seismic_response as sr
from seismic_response import (
    FIR,
    Channel,
    Decimation,
    Gain,
    IIRCoefficients,
    PolesZeros,
    Polynomial,
    ResponseList,
    Stage,
)

TWO_PI = 2 * math.pi
VEL = "M/S - Velocity"
VOLTS = "V - Volts"
COUNTS = "COUNTS - Counts"


def chan(*stages, ident=("XX", "TEST", "00", "BHZ")):
    return Channel(*ident, start_time=datetime(2000, 1, 1, tzinfo=timezone.utc), stages=list(stages))


def lowpass(corner_hz=1.0, gain=1.0, units=(VEL, VOLTS)):
    """H(f) = 1 / (1 + i f/fc): a single pole at -2*pi*fc, normalized to unity DC gain."""
    p = -TWO_PI * corner_hz
    return Stage(
        1, units[0], units[1],
        PolesZeros("laplace_rad", [], [complex(p, 0)], a0=-p, a0_freq=0.0),
        gain=Gain(gain, 0.0),
    )


# ------------------------------------------------------------------ analytic responses


def test_single_pole_lowpass_matches_analytic_formula():
    c = chan(lowpass(2.0))
    freqs = [0.1, 1.0, 2.0, 10.0]
    res = c.evaluate(freqs, unit="def")
    for f, v in zip(freqs, res.values):
        assert v == pytest.approx(1 / complex(1, f / 2.0), rel=1e-12)
    assert abs(res.values[2]) == pytest.approx(1 / math.sqrt(2))
    assert res.phases()[2] == pytest.approx(-45.0)


def test_hertz_poles_equal_radian_poles_after_scaling():
    """Poles in Hz at fc is the same filter as poles in rad/s at 2*pi*fc."""
    rad = chan(lowpass(1.5))
    hz = chan(Stage(1, VEL, VOLTS, PolesZeros("laplace_hz", [], [complex(-1.5, 0)], a0=1.5, a0_freq=0.0),
                    gain=Gain(1.0, 0.0)))
    f = [0.05, 0.7, 1.5, 6.0]
    for a, b in zip(rad.evaluate(f, unit="def").values, hz.evaluate(f, unit="def").values):
        assert a == pytest.approx(b, rel=1e-12)


def test_boxcar_fir_matches_closed_form():
    # 4-tap moving average at 100 Hz: |H| = |sin(4*pi*f/100) / (4*sin(pi*f/100))|
    n = 4
    stage = Stage(1, COUNTS, COUNTS, FIR("A", [0.25] * n), Decimation(100.0, 1), Gain(1.0, 0.0))
    c = chan(stage)
    for f in (1.0, 7.5, 20.0):
        h = abs(c.evaluate([f], unit="def").values[0])
        assert h == pytest.approx(abs(math.sin(n * math.pi * f / 100) / (n * math.sin(math.pi * f / 100))), rel=1e-9)


def test_symmetric_fir_has_zero_phase_and_asymmetric_is_linear_phase_corrected():
    sym = Stage(1, COUNTS, COUNTS, FIR("C", [0.1, 0.4]), Decimation(100.0, 1), Gain(1.0, 0.0))
    res = chan(sym).evaluate([3.0, 11.0], unit="def")
    assert all(abs(p) < 1e-9 for p in res.phases())  # symmetric FIR: no phase

    # The same filter written as a full asymmetric FIR is recognized as symmetric
    full = Stage(1, COUNTS, COUNTS, FIR("A", [0.1, 0.4, 0.4, 0.1]), Decimation(100.0, 1), Gain(1.0, 0.0))
    for a, b in zip(res.values, chan(full).evaluate([3.0, 11.0], unit="def").values):
        assert a == pytest.approx(b, rel=1e-12)


def test_iir_coefficients_match_direct_evaluation():
    num, den = [0.2, 0.4, 0.2], [1.0, -0.5, 0.25]
    c = chan(Stage(1, COUNTS, COUNTS, IIRCoefficients(num, den), Decimation(100.0, 1), Gain(1.0, 0.0)))
    f = 12.0
    w = TWO_PI * f / 100.0

    def transform(coeffs):
        return sum(a * complex(math.cos(-k * w), math.sin(-k * w)) for k, a in enumerate(coeffs))

    assert c.evaluate([f], unit="def").values[0] == pytest.approx(transform(num) / transform(den), rel=1e-12)


def test_polynomial_response_follows_the_maclaurin_derivative():
    # y = c0 + c1 x + c2 x^2  ->  slope at x is c1 + 2 c2 x
    poly = Stage(1, VOLTS, COUNTS, Polynomial([0.5, 0.1, 0.02]))
    c = chan(poly)
    assert c.evaluate([1.0], unit="def", b62_x=2.0).values[0] == pytest.approx(0.1 + 2 * 0.02 * 2.0)
    assert c.evaluate([1.0], unit="def").values[0] == pytest.approx(0.1)  # default b62_x is 0
    assert c.evaluate([1.0], unit="def", b62_x=-2.0).values[0] == pytest.approx(0.1 - 2 * 0.02 * 2.0)


def test_polynomial_negative_slope_keeps_its_sign():
    negative = chan(Stage(1, VOLTS, COUNTS, Polynomial([0.0, -0.3])))
    v = negative.evaluate([1.0], unit="def", b62_x=1.0).values[0]
    assert v == complex(-0.3, 0.0)


@pytest.mark.parametrize("unit", ["vel", "dis", "acc"])
@pytest.mark.parametrize("input_units", ["PA - Pascals", "C", "C - Degrees Centigrade", "T", "DEGF", "FURLONGS"])
def test_non_ground_motion_input_is_refused_when_converting(input_units, unit):
    c = chan(lowpass(1.0, units=(input_units, VOLTS)))
    freqs = [0.1, 1.0, 10.0]
    with pytest.raises(sr.ResponseValueError, match="not ground motion"):
        c.evaluate(freqs, unit=unit)
    assert len(c.evaluate(freqs, unit="def").values) == len(freqs)


def test_units_come_from_the_first_stage_that_has_them():
    disp = Stage(2, "M - Displacement", VOLTS, lowpass(1.0).filter, gain=Gain(1.0, 0.0))
    c = chan(Stage(1, gain=Gain(2.0, 0.0)), disp)
    w = TWO_PI * 0.5
    ref = c.evaluate([0.5], unit="def").values[0]
    assert c.evaluate([0.5], unit="vel").values[0] == pytest.approx(ref * complex(0, -1 / w))


@pytest.mark.parametrize("unit", ["vel", "dis", "acc"])
def test_overall_sensitivity_units_do_not_select_the_conversion(unit):
    c = chan(Stage(1, gain=Gain(2.0, 1.0)), Stage(2, gain=Gain(5.0, 1.0)),
             Stage(0, "M/S - Velocity", COUNTS, gain=Gain(10.0, 1.0)))
    with pytest.raises(sr.ResponseValueError, match="none documented"):
        c.evaluate([0.1, 1.0, 10.0], unit=unit)
    assert c.evaluate([0.1, 1.0, 10.0], unit="def").values == [10.0 + 0j] * 3


def _pz_channel(zeros, poles, gain_freq=5.0):
    pz = PolesZeros("laplace_rad", zeros, poles, a0=1.0, a0_freq=5.0)
    return chan(Stage(1, VEL, VOLTS, pz, gain=Gain(1.0, gain_freq)))


def test_pole_and_zero_at_the_evaluation_frequency_cancel():
    w = TWO_PI * 2.0
    plain = _pz_channel([0j], [complex(-0.3, 0.3)])
    paired = _pz_channel([0j, complex(0, w)], [complex(-0.3, 0.3), complex(0, w)])
    freqs = [1.0, 2.0, 3.0]
    assert paired.evaluate(freqs, unit="def").values == pytest.approx(plain.evaluate(freqs, unit="def").values)


def test_pole_at_the_evaluation_frequency_without_a_zero_is_infinite():
    c = _pz_channel([0j], [complex(0, TWO_PI * 2.0)])
    res = c.evaluate([1.0, 2.0, 3.0], unit="def")
    assert res.values[1] == complex(math.inf, 0.0)
    assert math.isfinite(res.values[0].real) and math.isfinite(res.values[2].real)
    assert any("pole at 2 Hz" in w for w in res.info.warnings)


def test_normalizing_at_a_pole_of_an_analog_filter_is_an_error():
    c = _pz_channel([0j], [complex(0, TWO_PI * 2.0)], gain_freq=2.0)
    with pytest.raises(sr.ResponseValueError, match="pole of the analog filter"):
        c.evaluate([1.0], unit="def")


def test_normalizing_above_nyquist_warns():
    pz = PolesZeros("z", [1 + 0j], [0.9938 + 0j], a0=1.0, a0_freq=10.0)
    stage = Stage(1, COUNTS, COUNTS, pz, Decimation(1.0, 1), Gain(1.0, 10.0))
    c = chan(stage, Stage(0, gain=Gain(1.0, 0.25)))
    res = c.evaluate([0.1], unit="def")
    assert any("above its Nyquist frequency of 0.5 Hz" in w for w in res.info.warnings)


def test_response_list_replaces_the_requested_frequencies():
    lst = Stage(1, VEL, COUNTS, ResponseList([0.1, 1.0, 10.0], [2.0, 4.0, 8.0], [0.0, 90.0, -90.0]))
    c = chan(lst, Stage(0, gain=Gain(1.0, 1.0)))
    res = c.evaluate([5.0, 6.0], unit="def")  # request ignored, as in evalresp
    assert res.frequencies == [0.1, 1.0, 10.0]
    assert res.values[1] == pytest.approx(4j)
    assert abs(res.values[2]) == pytest.approx(8.0)


# ------------------------------------------------------------------ normalization and sensitivity


def test_stage_gain_is_renormalized_to_the_sensitivity_frequency(anmo):
    """With gain/A0 at 0.02 Hz the response there equals the product of stage gains."""
    res = anmo[0].evaluate([0.02], unit="def")
    assert abs(res.values[0]) == pytest.approx(res.info.calculated_sensitivity, rel=0.02)
    assert res.info.sensitivity_frequency == 0.02
    assert res.info.reported_sensitivity == 864730000.0


def test_normalization_changes_stage_gain_when_frequencies_differ():
    # A0 normalized at 1 Hz but gain stated at 2 Hz: the gain is restated at the sensitivity frequency
    pz = PolesZeros("laplace_rad", [0j], [complex(-TWO_PI, 0)], a0=TWO_PI, a0_freq=1.0)
    c = chan(Stage(1, VEL, VOLTS, pz, gain=Gain(100.0, 2.0)), Stage(0, gain=Gain(100.0, 1.0)))
    res = c.evaluate([1.0], unit="def")
    assert abs(res.values[0]) == pytest.approx(res.info.calculated_sensitivity)
    assert res.info.stages[0].gain != 100.0  # the gain was restated at 1 Hz


def test_total_sensitivity_option_uses_the_reported_value(anmo):
    chan0 = anmo[0]
    a = chan0.evaluate([1.0], unit="def")
    b = chan0.evaluate([1.0], unit="def", use_total_sensitivity=True)
    ratio = abs(b.values[0]) / abs(a.values[0])
    assert ratio == pytest.approx(864730000.0 / a.info.calculated_sensitivity)


def test_input_not_modified_by_evaluation(anmo):
    before = repr(anmo[0])
    anmo[0].evaluate([0.01, 1.0], unit="vel")
    assert repr(anmo[0]) == before


# ------------------------------------------------------------------ units


def test_unit_conversion_relations(anmo):
    """dis = vel * (i w), acc = vel / (i w) for a velocity response (counts per unit input)."""
    f = [0.05, 0.5, 5.0]
    vel = anmo[0].evaluate(f, unit="vel").values
    dis = anmo[0].evaluate(f, unit="dis").values
    acc = anmo[0].evaluate(f, unit="acc").values
    for fr, v, d, a in zip(f, vel, dis, acc):
        w = TWO_PI * fr
        assert d == pytest.approx(v * 1j * w, rel=1e-12)
        assert a == pytest.approx(v / (1j * w), rel=1e-12)


def test_nanometre_input_units_are_scaled_to_metres():
    pz = PolesZeros("laplace_rad", [], [complex(-TWO_PI, 0)], a0=TWO_PI, a0_freq=0.0)
    metres = chan(Stage(1, "M/S - v", VOLTS, pz, gain=Gain(1.0, 0.0)))
    nano = chan(Stage(1, "NM/S - v", VOLTS, pz, gain=Gain(1.0, 0.0)))
    a = metres.evaluate([0.5], unit="vel").values[0]
    b = nano.evaluate([0.5], unit="vel").values[0]
    assert b == pytest.approx(a * 1e9)  # counts per nm/s -> counts per m/s


def test_default_unit_leaves_response_untouched(anmo):
    d = anmo[0].evaluate([0.3], unit="def").values[0]
    v = anmo[0].evaluate([0.3], unit="vel").values[0]
    assert d == pytest.approx(v)  # the documented unit already is velocity


def test_bad_unit_option_is_rejected(anmo):
    with pytest.raises(ValueError, match="Unit must be"):
        anmo[0].evaluate([1.0], unit="furlongs")


# ------------------------------------------------------------------ stage selection, per-stage output


def test_stage_selection(anmo):
    c = anmo[0]
    only_first = c.evaluate([1.0], unit="def", stages=(1, 1)).values[0]
    # Stage 1 alone is far smaller than the full response (no digitizer gain)
    full = abs(c.evaluate([1.0], unit="def").values[0])
    assert abs(only_first) < full / 1000
    # A range given as (first, None) runs to the last stage
    assert c.evaluate([1.0], unit="def", stages=(1, None)).values[0] == pytest.approx(
        c.evaluate([1.0], unit="def").values[0]
    )
    with pytest.raises(sr.ResponseValueError, match="no matching stages"):
        c.evaluate([1.0], stages=(20, 30))


def test_per_stage_values_multiply_to_the_total(anmo):
    f = [0.01, 0.1, 1.0, 5.0]
    res = anmo[0].evaluate(f, unit="vel", per_stage=True)
    assert sorted(res.stage_values) == [1, 2, 3, 4, 5, 6]
    for i, total in enumerate(res.values):
        product = 1 + 0j
        for seq in res.stage_values:
            product *= res.stage_values[seq][i]
        assert product == pytest.approx(total, rel=1e-10)


# ------------------------------------------------------------------ validation errors


@pytest.mark.parametrize(
    "stages, message",
    [
        ([Stage(1, VEL, VOLTS, PolesZeros("laplace_rad", [], [-1 + 0j], 1.0, 0.0), gain=Gain(0.0, 1.0))], "zero stage gain"),
        ([Stage(1, VEL, COUNTS, FIR("A", [1.0]), gain=Gain(1.0, 0.0))], "decimation"),
        ([Stage(1, VEL, VOLTS, PolesZeros("laplace_rad", [], [-1 + 0j], 1.0, 0.0))], "no stage gain"),
        (
            [
                Stage(1, VEL, VOLTS, PolesZeros("laplace_rad", [], [-1 + 0j], 1.0, 0.0), gain=Gain(1, 1)),
                Stage(2, "M - m", COUNTS, FIR("A", [1.0]), Decimation(10.0, 1), Gain(1, 0)),
            ],
            "units mismatch",
        ),
        ([Stage(1, "FURLONGS", VOLTS, PolesZeros("laplace_rad", [], [-1 + 0j], 1.0, 0.0), gain=Gain(1, 1))], "not ground motion"),
        ([], "no response stages"),
    ],
)
def test_invalid_responses_are_reported(stages, message):
    with pytest.raises(sr.ResponseValueError, match=message):
        chan(*stages).evaluate([1.0], unit="vel")


def test_unit_mismatch_is_ignored_when_using_documented_units():
    stages = [
        Stage(1, VEL, VOLTS, PolesZeros("laplace_rad", [], [-1 + 0j], 1.0, 1.0), gain=Gain(2, 1)),
        Stage(2, "M - m", COUNTS, FIR("A", [1.0]), Decimation(10.0, 1), Gain(1, 0)),
    ]
    assert chan(*stages).evaluate([1.0], unit="def")  # evalresp skips the continuity check for 'def'


def test_estimated_delay_option_changes_asymmetric_fir_phase():
    """Asymmetric FIRs get a time shift: default = correction_applied - calculated delay,
    with the option = estimated delay.  The magnitude is unaffected."""
    sample_interval = 0.01
    asym = Stage(1, COUNTS, COUNTS, FIR("A", [0.5, 0.3, 0.15, 0.05]),
                 Decimation(100.0, 1, 0, estimated_delay=0.03, correction_applied=0.0), Gain(1.0, 0.0))
    c = chan(asym)
    f = 10.0
    a = c.evaluate([f], unit="def")
    b = c.evaluate([f], unit="def", use_estimated_delay=True)
    assert abs(a.values[0]) == pytest.approx(abs(b.values[0]))

    calculated_delay = (4 - 1) / 2.0 * sample_interval  # 4 taps
    default_shift = 0.0 - calculated_delay
    estimated_shift = 0.03
    expected = math.degrees(TWO_PI * f * (estimated_shift - default_shift)) % 360.0
    assert (b.phases()[0] - a.phases()[0]) % 360.0 == pytest.approx(expected, abs=1e-6)


def test_warnings_are_collected_not_raised():
    # Reported sensitivity differs from the product of gains by >5%
    pz = PolesZeros("laplace_rad", [], [complex(-TWO_PI, 0)], a0=TWO_PI, a0_freq=1.0)
    c = chan(Stage(1, VEL, VOLTS, pz, gain=Gain(10.0, 1.0)), Stage(0, gain=Gain(20.0, 1.0)))
    res = c.evaluate([1.0], unit="def")
    assert any("differ by more than 5 percent" in w for w in res.info.warnings)


# ------------------------------------------------------------------ frequencies and phase


def test_generate_frequencies():
    assert sr.generate_frequencies(0.01, 10, 3) == pytest.approx([0.01, 0.31622776601683794, 10.0])
    assert sr.generate_frequencies(1, 5, 5, linear=True) == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sr.generate_frequencies(2.0) == [2.0]  # evalresp: max defaults to min, one point
    assert sr.generate_frequencies(10, 1, 2) == pytest.approx([1.0, 10.0])  # reversed limits are swapped
    with pytest.raises(sr.ResponseValueError, match="zero frequency"):
        sr.generate_frequencies(0, 10, 5)
    assert sr.generate_frequencies(0, 10, 3, linear=True) == [0.0, 5.0, 10.0]


def test_phase_unwrapping():
    # A phase ramp of -100 degrees per step wraps around +-180; unwrapping restores a monotone ramp
    values = [complex(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in (-100, -200, -300, -400)]
    wrapped = sr.phase_degrees(values)
    assert min(wrapped) > -180
    unwrapped = sr.phase_degrees(values, unwrap=True)
    steps = [b - a for a, b in zip(unwrapped, unwrapped[1:])]
    assert all(s == pytest.approx(-100.0) for s in steps)
    assert 0 <= unwrapped[0] < 360  # evalresp forces the first value into [0, 360)
    assert sr.phase_degrees([0j]) == [0.0]  # the 1e-200 guard keeps atan2 defined


def _list_channel(freqs, amps, phases):
    c = chan(Stage(1, "M/S - Velocity", COUNTS, ResponseList(freqs, amps, phases), gain=Gain(1.0, 1.0)))
    c.stages.append(Stage(0, None, None, None, gain=Gain(1.0, 1.0)))
    return c


def test_list_interpolation_reproduces_knots_and_clips_to_the_list_range():
    freqs = [1.0, 2.0, 4.0, 8.0]
    c = _list_channel(freqs, [1.0, 2.0, 3.0, 4.0], [0.0, 10.0, 20.0, 30.0])
    res = c.evaluate([0.5, 2.0, 8.0, 16.0], unit="def", interpolate_list=True)
    assert res.frequencies == [2.0, 8.0]
    assert abs(res.values[0]) == pytest.approx(2.0)
    assert abs(res.values[1]) == pytest.approx(4.0)
    assert len(res.info.warnings) == 2
    # Without interpolation the listed frequencies are reported
    assert c.evaluate([3.0], unit="def").frequencies == freqs


def test_list_interpolation_keeps_requests_within_rounding_of_the_limits():
    c = _list_channel([1.0, 2.0, 4.0, 8.0], [1.0, 2.0, 3.0, 4.0], [0.0, 10.0, 20.0, 30.0])
    res = c.evaluate([1.0 * (1 - 1e-12), 4.0, 8.0 * (1 + 1e-12)], unit="def", interpolate_list=True)
    assert len(res.values) == 3
    assert abs(res.values[0]) == pytest.approx(1.0)
    assert abs(res.values[2]) == pytest.approx(4.0)
    assert not res.info.warnings
    res = c.evaluate([1.0 * (1 - 1e-6), 4.0], unit="def", interpolate_list=True)
    assert res.frequencies == [4.0]


def test_list_interpolation_is_exact_for_power_law_data_on_uneven_knots():
    freqs = [1.0, 1.5, 4.0, 5.0, 8.0]
    c = _list_channel(freqs, [3.0 * f**-1.5 for f in freqs], [40.0 * math.log10(f) for f in freqs])
    req = [1.2, 2.0, 4.5, 7.0]
    res = c.evaluate(req, unit="def", interpolate_list=True)
    assert [abs(v) for v in res.values] == pytest.approx([3.0 * f**-1.5 for f in req])
    assert sr.phase_degrees(res.values) == pytest.approx([40.0 * math.log10(f) for f in req])


def test_cubic_spline_has_continuous_slope_on_uneven_knots():
    from seismic_response.evaluate import _cubic_spline

    t = [0.0, 0.1, 0.5, 0.6, 1.5, 3.0]
    y = [math.sin(x) for x in t]
    eps = 1e-6
    for k in t[1:-1]:
        left, right = _cubic_spline(t, y, [k - eps, k]), _cubic_spline(t, y, [k, k + eps])
        assert (left[1] - left[0]) / eps == pytest.approx((right[1] - right[0]) / eps, rel=1e-3)
    assert _cubic_spline(t, y, t) == pytest.approx(y)


def test_list_interpolation_falls_back_to_linear_space_for_non_positive_values():
    c = _list_channel([1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 2.0, 3.0], [0.0, 0.0, 0.0, 0.0])
    res = c.evaluate([1.5, 3.5], unit="def", interpolate_list=True)
    assert any("linear space" in w for w in res.info.warnings)
    assert [abs(v) for v in res.values] == pytest.approx([0.5, 2.5])


def test_list_interpolation_wraps_phase_and_keeps_amplitude_positive():
    freqs = [1.0, 2.0, 3.0, 4.0]
    c = _list_channel(freqs, [0.01, 1.0, 1.0, 0.01], [170.0, -170.0, -150.0, -130.0])
    res = c.evaluate([1.5, 2.5, 3.5], unit="def", interpolate_list=True)
    assert all(abs(v) > 0 for v in res.values)
    assert all(-180.0 <= p <= 180.0 for p in sr.phase_degrees(res.values))


def test_list_interpolation_rejects_unusable_input():
    c = _list_channel([1.0, 1.0, 2.0], [1.0, 1.0, 1.0], [0.0, 0.0, 0.0])
    with pytest.raises(sr.ResponseValueError, match="strictly increasing"):
        c.evaluate([1.5], interpolate_list=True)
    c = _list_channel([1.0, 2.0], [1.0, 1.0], [0.0, 0.0])
    with pytest.raises(sr.ResponseValueError, match="within requested"):
        c.evaluate([5.0], interpolate_list=True)


def test_list_interpolation_leaves_other_responses_unchanged(anmo):
    f = [0.5, 2.0]
    assert anmo[0].evaluate(f, interpolate_list=True).values == anmo[0].evaluate(f).values


def test_default_unit_is_the_documented_unit(anmo):
    """Unlike evalresp (which defaults to velocity), the default is ``def``."""
    f = [0.5, 2.0]
    assert anmo[0].evaluate(f).values == anmo[0].evaluate(f, unit="def").values
    c = chan(Stage(1, "M/S**2 - Acceleration", VOLTS, PolesZeros("laplace_rad", [], [-1 + 0j], 1.0, 0.0), gain=Gain(1, 1)))
    assert c.evaluate([1.0]).values == c.evaluate([1.0], unit="def").values
    assert c.evaluate([1.0]).values != c.evaluate([1.0], unit="vel").values


def _unit_channel(units: str):
    from seismic_response.model import LAPLACE_RADIANS, Channel, Gain, PolesZeros, Stage

    pz = PolesZeros(LAPLACE_RADIANS, [0j, 0j], [complex(-1, 1), complex(-1, -1)], 1.0, 1.0)
    return Channel("XX", "T", "", "BHZ", stages=[Stage(1, units, "COUNTS", pz, gain=Gain(100.0, 1.0))])


def test_double_s_acceleration_units_convert_like_double_star():
    """evalresp would read ``M/S/S`` as velocity and skip the conversion; here it is acceleration."""
    freqs = [0.1, 1.0, 5.0]
    ref = sr.evaluate(_unit_channel("M/S**2 - Acceleration"), freqs, unit="vel").values
    got = sr.evaluate(_unit_channel("M/S/S - Acceleration"), freqs, unit="vel").values
    assert got == pytest.approx(ref)
    assert got != pytest.approx(sr.evaluate(_unit_channel("M/S - Velocity"), freqs, unit="vel").values)


def test_unrecognized_motion_like_units_fail_loudly_when_converting():
    ch = _unit_channel("MBAR - millibar")
    with pytest.raises(sr.ResponseValueError, match="not ground motion"):
        sr.evaluate(ch, [1.0], unit="vel")
    assert len(sr.evaluate(ch, [1.0], unit="def").values) == 1  # default units never classify

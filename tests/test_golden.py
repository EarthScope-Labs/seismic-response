"""Results must match the C evalresp program (golden values captured from it)."""

import cmath
import json
from pathlib import Path

import pytest
from conftest import DATA, load_fixture

import seismic_response as sr
from seismic_response.timeutil import parse_iso_time

GOLDEN = json.loads((DATA / "golden" / "golden.json").read_text(encoding="utf-8"))


def _id(entry):
    case = entry["case"]
    opts = [case["unit"], "x".join(map(str, case["freqs"][:3]))]
    opts += [k for k in ("ts", "estdelay", "il") if case.get(k)]
    if "b62_x" in case:
        opts.append(f"b62_x{case['b62_x']}")
    if case.get("stages"):
        opts.append("stages%s-%s" % tuple(case["stages"]))
    return f"{Path(entry['fixture']).stem}:{entry['nslc']}@{entry['time'][:10]}:{'/'.join(opts)}"


def _compare(rows_c, values, freqs, rtol=2e-6, floor=1e-9):
    """Relative error per point.  A point where both values are below ``floor`` of the peak
    (such as a FIR response at Nyquist) is numerical noise and is not compared."""
    assert len(rows_c) == len(values)
    finite = [abs(complex(r, i)) for _, r, i in rows_c if cmath.isfinite(complex(r, i))]
    peak = max(finite, default=0.0) or 1.0
    worst = 0.0
    for (fc, rc, ic), fp, vp in zip(rows_c, freqs, values):
        assert fp == pytest.approx(fc, rel=1e-5)
        ref = complex(rc, ic)
        if ref == vp:  # includes equal infinities
            continue
        assert cmath.isfinite(ref) and cmath.isfinite(vp), f"non-finite value differs at {fc}: {ref} vs {vp}"
        mag = max(abs(ref), abs(vp))
        if mag < floor * peak:
            continue  # numerical noise, e.g. an evaluation point on a zero of a digital filter
        worst = max(worst, abs(ref - vp) / mag)
    assert worst <= rtol, f"max relative error {worst:.3e}"


@pytest.mark.parametrize("entry", GOLDEN, ids=[_id(e) for e in GOLDEN])
def test_matches_c_evalresp(entry):
    net, sta, loc, cha = entry["nslc"].split(".")
    chans = load_fixture(entry["fixture"])
    found = sr.select_channels(
        chans, network=[net], station=[sta], location=[loc], channel=[cha], time=parse_iso_time(entry["time"])
    )
    assert found, "channel not selected"

    case = entry["case"]
    mn, mx, n, spacing = case["freqs"]
    freqs = sr.generate_frequencies(mn, mx, n, linear=spacing == "lin")
    stages = case.get("stages")
    if stages and stages[1] is None:  # evalresp: "-stage N" selects only stage N
        stages = (stages[0], stages[0])
    res = sr.evaluate(
        found[0],
        freqs,
        unit=case["unit"],
        stages=stages,
        use_total_sensitivity=case.get("ts", False),
        use_estimated_delay=case.get("estdelay", False),
        b62_x=case.get("b62_x", 0.0),
        interpolate_list=case.get("il", False),
    )
    _compare(entry["rows"], res.values, res.frequencies)


def test_golden_data_is_substantial():
    # Guard against an accidentally truncated golden file
    assert len(GOLDEN) > 300
    assert {e["fixture"].split("/")[0] for e in GOLDEN} == {"real", "synthetic", "evalresp"}

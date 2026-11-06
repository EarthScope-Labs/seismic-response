"""Results must match the C evalresp program (golden values captured from it)."""

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
    if case.get("stages"):
        opts.append("stages%s-%s" % tuple(case["stages"]))
    return f"{Path(entry['fixture']).stem}:{entry['nslc']}@{entry['time'][:10]}:{'/'.join(opts)}"


def _compare(rows_c, values, freqs, rtol=2e-6, floor=1e-9):
    """Relative error per point, but never relative to less than ``floor`` of the peak
    (values ~1e-18 of the peak, such as a FIR response at Nyquist, are numerical noise)."""
    assert len(rows_c) == len(values)
    peak = max(abs(complex(r, i)) for _, r, i in rows_c) or 1.0
    worst = 0.0
    for (fc, rc, ic), fp, vp in zip(rows_c, freqs, values):
        assert fp == pytest.approx(fc, rel=1e-5)
        ref = complex(rc, ic)
        mag = max(abs(ref), abs(vp), floor * peak)
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
        b62_x=2.0,
        interpolate_list=case.get("il", False),
    )
    _compare(entry["rows"], res.values, res.frequencies)


def test_golden_data_is_substantial():
    # Guard against an accidentally truncated golden file
    assert len(GOLDEN) > 100
    assert {e["fixture"].split("/")[0] for e in GOLDEN} == {"real", "synthetic"}

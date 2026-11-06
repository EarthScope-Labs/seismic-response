#!/usr/bin/env python3
"""Compare seismic_response against a built C evalresp binary.

Runs the C program and this package on the same inputs across a matrix of
options and reports any differences beyond print precision.  Developer tool:

    python tools/compare_evalresp.py /path/to/evalresp/src/evalresp FILE [FILE ...]

Each channel epoch of each file is tested at a time inside the epoch.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import seismic_response as sr
from seismic_response.errors import SeismicResponseError

CASES = [
    dict(unit="vel", freqs=(0.001, 40, 25, "log")),
    dict(unit="dis", freqs=(0.01, 20, 17, "log")),
    dict(unit="acc", freqs=(0.01, 20, 17, "lin")),
    dict(unit="def", freqs=(0.005, 15, 21, "log")),
    dict(unit="vel", freqs=(0.02, 0.02, 1, "log")),
    dict(unit="vel", freqs=(0.001, 40, 25, "log"), ts=True),
    dict(unit="vel", freqs=(0.001, 40, 25, "log"), estdelay=True),
    dict(unit="vel", freqs=(0.001, 40, 25, "log"), stages=(1, None)),
    dict(unit="vel", freqs=(0.001, 40, 25, "log"), stages=(2, 3)),
    dict(unit="vel", freqs=(0.001, 40, 25, "log"), stages=(1, 1)),
]


def _run_c_once(binary, file, chan, when, case, workdir):
    year, jday = when.year, when.timetuple().tm_yday
    mn, mx, n, spacing = case["freqs"]
    cmd = [binary, chan.station, chan.channel, str(year), str(jday), repr(mn), repr(mx), str(n),
           "-f", str(file), "-u", case["unit"], "-s", spacing, "-r", "cs",
           "-t", when.strftime("%H:%M:%S"), "-n", chan.network or "*", "-l", chan.location or "??",
           "-b62_x", "2.0"]
    if case.get("ts"):
        cmd.append("-ts")
    if case.get("estdelay"):
        cmd.append("-use-estimated-delay")
    if case.get("stages"):
        a, b = case["stages"]
        cmd += ["-stage", str(a)] + ([str(b)] if b is not None else [])
    out = Path(workdir) / f"SPECTRA.{chan.network}.{chan.station}.{chan.location}.{chan.channel}"
    if out.exists():
        out.unlink()
    p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    if p.returncode != 0 or not out.exists():
        return None, (p.stdout + p.stderr).strip() or f"rc={p.returncode}", p.returncode
    rows = [tuple(map(float, l.split())) for l in out.read_text().splitlines() if l.strip()]
    return rows, "", 0


def run_c(binary, source, index, chan, when, case, workdir):
    """Run C on the whole file; if the C program crashes, retry with only this channel's block."""
    whole = Path(workdir) / "whole.resp"
    whole.write_text(source.text)
    rows, err, rc = _run_c_once(binary, whole, chan, when, case, workdir)
    if rows is None and rc < 0 or (rows is None and rc >= 128):
        block = Path(workdir) / "block.resp"
        block.write_text(source.blocks[index])
        rows, err, rc = _run_c_once(binary, block, chan, when, case, workdir)
    return rows, err


class Source:
    """A response file prepared for comparison: its RESP text, per-channel blocks and channels."""

    def __init__(self, path, binary):
        self.path = path
        data = Path(path).read_bytes()
        self.xml_channels = None
        if sr.looks_like_stationxml(data[:1024]):
            self.xml_channels = sr.parse_stationxml(data)
            xml2resp = Path(binary).parent / "xml2resp"
            p = subprocess.run([str(xml2resp)], input=data, capture_output=True)
            if p.returncode != 0:
                raise RuntimeError(f"xml2resp failed: {p.stderr.decode()[:200]}")
            self.text = p.stdout.decode("latin-1")
        else:
            self.text = data.decode("utf-8", errors="replace")
        parts = re.split(r"(?m)^(?=B050F03)", self.text)
        self.blocks = parts[1:]
        self.channels = sr.parse_resp(self.text, Path(path).name)
        assert len(self.blocks) == len(self.channels)


def _close(a, b, rtol=2e-5):
    return abs(a - b) <= rtol * max(abs(a), abs(b)) + 1e-300


def structural_diffs(xml_chans, resp_chans):
    """Compare the XML reading with C's xml2resp RESP output (which prints 6 significant digits)."""
    diffs = []
    if len(xml_chans) != len(resp_chans):
        return [f"channel count: xml={len(xml_chans)} resp={len(resp_chans)}"]
    for x, r in zip(xml_chans, resp_chans):
        tag = f"{x.nslc} {x.start_time}"
        if (x.nslc, x.start_time, x.end_time) != (r.nslc, r.start_time, r.end_time):
            diffs.append(f"{tag}: identity/epoch differs: {r.nslc} {r.start_time} {r.end_time} vs {x.end_time}")
        # xml2resp lists the instrument sensitivity (stage 0) last, as does the XML reader
        if [s.sequence for s in x.stages] != [s.sequence for s in r.stages]:
            diffs.append(f"{tag}: stage sequence {[s.sequence for s in x.stages]} vs {[s.sequence for s in r.stages]}")
            continue
        for sx, sr_ in zip(x.stages, r.stages):
            where = f"{tag} stage {sx.sequence}"
            if type(sx.filter) is not type(sr_.filter):
                diffs.append(f"{where}: filter {type(sx.filter).__name__} vs {type(sr_.filter).__name__}")
                continue
            fx, fr = sx.filter, sr_.filter
            if fx is not None:
                for attr in ("kind", "symmetry"):
                    if getattr(fx, attr, None) != getattr(fr, attr, None):
                        diffs.append(f"{where}: {attr} differs")
                for attr in ("zeros", "poles", "coefficients", "numerators", "denominators",
                             "frequencies", "amplitudes", "phases"):
                    a, b = getattr(fx, attr, None), getattr(fr, attr, None)
                    if a is None:
                        continue
                    if len(a) != len(b) or any(not _close(complex(u), complex(v)) for u, v in zip(a, b)):
                        diffs.append(f"{where}: {attr} differ")
                for attr in ("a0", "a0_freq"):
                    if hasattr(fx, attr) and not _close(getattr(fx, attr), getattr(fr, attr)):
                        diffs.append(f"{where}: {attr} {getattr(fx, attr)} vs {getattr(fr, attr)}")
            if (sx.gain is None) != (sr_.gain is None) or (
                    sx.gain and not (_close(sx.gain.value, sr_.gain.value)
                                     and _close(sx.gain.frequency, sr_.gain.frequency))):
                diffs.append(f"{where}: gain {sx.gain} vs {sr_.gain}")
            dx, dr = sx.decimation, sr_.decimation
            if (dx is None) != (dr is None):
                diffs.append(f"{where}: decimation presence differs")
            elif dx and not (_close(dx.input_sample_rate, dr.input_sample_rate) and dx.factor == dr.factor
                             and _close(dx.estimated_delay, dr.estimated_delay)
                             and _close(dx.correction_applied, dr.correction_applied)):
                diffs.append(f"{where}: decimation {dx} vs {dr}")
            def short(u):
                return (u or "").split(" -")[0].strip()  # name only; C writes "NAME - description"
            ux = short(sx.input_units), short(sx.output_units)
            ur = short(sr_.input_units), short(sr_.output_units)
            if sx.filter is not None and ux != ur:
                diffs.append(f"{where}: units {ux} vs {ur}")
    return diffs


def run_py(channels, chan, when, case):
    # Select by identifier and time exactly as C does (shortest overlapping epoch wins)
    found = sr.select_channels(channels, network=[chan.network], station=[chan.station],
                               location=[chan.location], channel=[chan.channel], time=when)
    if not found:
        raise sr.ChannelNotFoundError("no channel selected")
    chan = found[0]
    mn, mx, n, spacing = case["freqs"]
    freqs = sr.generate_frequencies(mn, mx, n, linear=spacing == "lin")
    stages = case.get("stages")
    # C semantics: -stage N alone selects only stage N
    if stages and stages[1] is None:
        stages = (stages[0], stages[0])
    res = sr.evaluate(chan, freqs, unit=case["unit"], stages=stages,
                      use_total_sensitivity=case.get("ts", False),
                      use_estimated_delay=case.get("estdelay", False), b62_x=2.0)
    return [(f, v.real, v.imag) for f, v in zip(res.frequencies, res.values)], ""


def close(a, b, rtol):
    scale = max(abs(a), abs(b))
    return abs(a - b) <= rtol * scale + 1e-300


def compare(rows_c, rows_p, rtol, floor=1e-9):
    """Compare complex spectra.

    Error at each point is relative to that point's magnitude, but never
    relative to less than ``floor`` times the peak magnitude: a value that is
    1e-18 of the peak (e.g. a FIR response at Nyquist) is numerical noise and
    legitimately differs between implementations.
    """
    if len(rows_c) != len(rows_p):
        return f"row count differs: C={len(rows_c)} py={len(rows_p)}"
    peak = max(abs(complex(r, i)) for _, r, i in rows_c) or 1.0
    worst = 0.0
    for (fc, rc, ic), (fp, rp, ip) in zip(rows_c, rows_p):
        if not close(fc, fp, 1e-5):
            return f"frequency differs {fc} vs {fp}"
        mag = max(abs(complex(rc, ic)), abs(complex(rp, ip)), floor * peak)
        err = abs(complex(rc, ic) - complex(rp, ip))
        worst = max(worst, err / mag)
    return None if worst <= rtol else f"max relative error {worst:.3e} > {rtol}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--rtol", type=float, default=2e-6)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--formats", action="store_true", help="compare FAP/AP text output instead of complex spectra")
    args = ap.parse_args()
    if args.formats:
        return format_main(args.binary, args.files)

    stats = dict(ok=0, bad=0, both_error=0, only_c_error=0, only_py_error=0, xml_structure_diffs=0)
    with tempfile.TemporaryDirectory() as work:
        for file in args.files:
            try:
                source = Source(file, args.binary)
            except (SeismicResponseError, RuntimeError) as exc:
                print(f"SKIP {file}: {exc}")
                continue
            if source.xml_channels is not None:
                diffs = structural_diffs(source.xml_channels, source.channels)
                stats["xml_structure_diffs"] += len(diffs)
                for d in diffs[:20]:
                    print(f"XML STRUCTURE {Path(file).name}: {d}")
                print(f"{Path(file).name}: StationXML vs xml2resp structure: "
                      f"{len(source.channels)} channels, {len(diffs)} differences")
            channels = source.channels
            for index, chan in enumerate(channels):
                if chan.start_time is None:
                    continue
                # A time inside the epoch; capped so open-ended epochs stay near the start
                # (C evalresp produces nothing for dates centuries in the future).
                end = chan.end_time or (chan.start_time + timedelta(days=400))
                half = (end - chan.start_time) / 2
                when = (chan.start_time + min(half, timedelta(days=60))).replace(microsecond=0)
                for case in CASES:
                    label = f"{Path(file).name} {chan.nslc} {when:%Y-%m-%d} {case}"
                    rows_c, err_c = run_c(args.binary, source, index, chan, when, case, work)
                    try:
                        rows_p, err_p = run_py(channels, chan, when, case)
                    except SeismicResponseError as exc:
                        rows_p, err_p = None, str(exc)
                    if rows_c is None and rows_p is None:
                        stats["both_error"] += 1
                        if args.v:
                            print(f"both error  {label}\n   C : {err_c[:150]}\n   py: {err_p[:150]}")
                    elif rows_c is None:
                        stats["only_c_error"] += 1
                        print(f"C ERROR ONLY {label}\n   C : {err_c[:200]}")
                    elif rows_p is None:
                        stats["only_py_error"] += 1
                        print(f"PY ERROR ONLY {label}\n   py: {err_p[:200]}")
                    else:
                        msg = compare(rows_c, rows_p, args.rtol)
                        if msg:
                            stats["bad"] += 1
                            print(f"MISMATCH {label}\n   {msg}")
                        else:
                            stats["ok"] += 1
    print(stats)
    return 1 if stats["bad"] or stats["only_py_error"] or stats["xml_structure_diffs"] else 0



# --------------------------------------------------------------------------- text formats
#
# The complex-spectra comparison above validates the numbers.  The FAP and AP
# text outputs add amplitude/phase conversion and phase unwrapping, checked here.

FORMAT_CASES = [
    dict(fmt="fap", extra=[], unwrap=True, freqs=(0.001, 40, 40, "log"), unit="vel"),
    dict(fmt="ap", extra=[], unwrap=False, freqs=(0.001, 40, 40, "log"), unit="vel"),
    dict(fmt="ap", extra=["-unwrap"], unwrap=True, freqs=(0.001, 40, 40, "log"), unit="vel"),
    dict(fmt="fap", extra=[], unwrap=True, freqs=(0.01, 20, 30, "lin"), unit="dis"),
]


def run_c_format(binary, text, chan, when, case, workdir):
    from pathlib import Path as _P
    mn, mx, n, spacing = case["freqs"]
    f = _P(workdir) / "fmt.resp"
    f.write_text(text)
    for old in _P(workdir).glob("[AFPS]*.*"):
        old.unlink()
    cmd = [binary, chan.station, chan.channel, str(when.year), str(when.timetuple().tm_yday), repr(mn), repr(mx),
           str(n), "-f", str(f), "-u", case["unit"], "-s", spacing, "-r", case["fmt"],
           "-t", when.strftime("%H:%M:%S"), "-n", chan.network or "*", "-l", chan.location or "??",
           "-b62_x", "2.0"] + case["extra"]
    p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    ident = f"{chan.network}.{chan.station}.{chan.location}.{chan.channel}"
    out = {}
    for prefix in ("FAP", "AMP", "PHASE"):
        path = _P(workdir) / f"{prefix}.{ident}"
        if path.exists():
            out[prefix] = [tuple(map(float, l.split())) for l in path.read_text().splitlines() if l.strip()]
    return out or None


def compare_formats(binary, source, index, chan, when, case, workdir, rtol=2e-6):
    from seismic_response.output import render
    found = sr.select_channels(source.channels, network=[chan.network], station=[chan.station],
                               location=[chan.location], channel=[chan.channel], time=when)
    mn, mx, n, spacing = case["freqs"]
    res = sr.evaluate(found[0], sr.generate_frequencies(mn, mx, n, linear=spacing == "lin"),
                      unit=case["unit"], b62_x=2.0)
    ref = run_c_format(binary, source.text, chan, when, case, workdir)
    if ref is None:
        ref = run_c_format(binary, source.blocks[index], chan, when, case, workdir)
    if ref is None:
        return "skip"
    mine = {}
    for name, text in render(res, case["fmt"], unwrap=case["extra"] == ["-unwrap"]):
        prefix = name.split(".")[0]
        mine[prefix] = [tuple(map(float, l.split())) for l in text.splitlines()]
    peak = max(res.amplitudes) or 1.0
    for prefix, rows_c in ref.items():
        rows_p = mine[prefix]
        if len(rows_c) != len(rows_p):
            return f"{prefix}: row count"
        for (rc, rp) in zip(rows_c, rows_p):
            amp_idx = 1 if prefix in ("FAP", "AMP") else None
            if amp_idx is not None and not close(rc[1], rp[1], max(rtol, 1e-5)) and abs(rc[1] - rp[1]) > 1e-9 * peak:
                return f"{prefix}: amplitude {rc} vs {rp}"
            if prefix in ("FAP", "PHASE"):
                amp = rc[1] if prefix == "FAP" else None
                if prefix == "FAP" and amp < 1e-6 * peak:
                    continue  # phase of a numerically-zero response is meaningless
                pc, pp = rc[-1], rp[-1]
                if abs(pc - pp) > 1e-5 * max(1.0, abs(pc)):
                    return f"{prefix}: phase {rc} vs {rp}"
    return None


def format_main(binary, files):
    stats = dict(ok=0, bad=0, skipped=0)
    with tempfile.TemporaryDirectory() as work:
        for file in files:
            source = Source(file, binary)
            for index, chan in enumerate(source.channels):
                end = chan.end_time or (chan.start_time + timedelta(days=400))
                when = (chan.start_time + min((end - chan.start_time) / 2, timedelta(days=60))).replace(microsecond=0)
                for case in FORMAT_CASES:
                    try:
                        msg = compare_formats(binary, source, index, chan, when, case, work)
                    except SeismicResponseError:
                        msg = "skip"
                    if msg == "skip":
                        stats["skipped"] += 1
                    elif msg:
                        stats["bad"] += 1
                        print(f"FORMAT MISMATCH {Path(file).name} {chan.nslc} {when:%Y-%m-%d} {case['fmt']} "
                              f"{case['extra']} {case['unit']}: {msg}")
                    else:
                        stats["ok"] += 1
    print(stats)
    return 1 if stats["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())

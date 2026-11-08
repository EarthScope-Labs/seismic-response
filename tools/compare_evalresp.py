#!/usr/bin/env python3
"""Compare seismic_response against a built C evalresp binary.

Runs the C program and this package on the same inputs across a matrix of
options and reports any differences beyond print precision.  Developer tool:

    python tools/compare_evalresp.py [--jobs N] /path/to/evalresp/src/evalresp PATH [PATH ...]

A PATH is a response file or a directory; a directory means every file beneath it.
Each channel epoch of each file is tested at a time inside the epoch.  StationXML
files are given to evalresp as they are; it detects the format itself.

With --jobs N, N files are compared in parallel, each parsed by the worker that
tests it; fewer files than N are split by channel epoch.  Output order and
results are the same as for a serial run.
"""

import argparse
import cmath
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

import seismic_response as sr
from seismic_response.errors import SeismicResponseError

# Library warnings stay on each result; the logger is silenced to keep output to mismatches.
logging.getLogger("seismic_response").setLevel(logging.ERROR)

LOG = (0.001, 40, 25, "log")

# Keys: unit, freqs (min, max, count, spacing), ts, estdelay, stages (first, last or None), il,
# b62_x, and only (file name substrings the case is restricted to).  A case with il or b62_x runs
# only on channels with a response list or polynomial; b62_x is otherwise left at the default.
CASES = [
    dict(unit="vel", freqs=LOG),
    dict(unit="dis", freqs=(0.01, 20, 17, "log")),
    dict(unit="acc", freqs=(0.01, 20, 17, "lin")),
    dict(unit="def", freqs=(0.005, 15, 21, "log")),
    dict(unit="def", freqs=LOG),
    dict(unit="def", freqs=(0.01, 20, 17, "lin")),
    dict(unit="vel", freqs=(0.02, 0.02, 1, "log")),
    dict(unit="vel", freqs=LOG, ts=True),
    dict(unit="vel", freqs=LOG, estdelay=True),
    dict(unit="vel", freqs=LOG, stages=(1, None)),
    dict(unit="vel", freqs=LOG, stages=(2, 3)),
    dict(unit="vel", freqs=LOG, stages=(1, 1)),
    dict(unit="vel", freqs=(0.005, 20, 30, "log"), il=True),
    dict(unit="def", freqs=(0.005, 20, 30, "log"), il=True),
    # 1e-10 (relative) outside the list's limits of 0.1 and 10 Hz, which evalresp treats as inside
    dict(unit="def", freqs=(0.09999999999, 10.000000001, 9, "log"), il=True, only=("station-list",)),
    dict(unit="def", freqs=LOG, b62_x=2.0),
    dict(unit="def", freqs=LOG, b62_x=-1.0),
    # evaluation points exactly on a pole (and a coinciding zero) at 2 Hz
    dict(unit="def", freqs=(1, 4, 4, "lin"), only=("pz_cancel", "pz_pole_only")),
]


def applies(case, chan, name):
    """Whether a case is meaningful for a channel of the file called ``name``."""
    if case.get("il") and not any(isinstance(s.filter, sr.ResponseList) for s in chan.stages):
        return False
    if "b62_x" in case and not any(isinstance(s.filter, sr.Polynomial) for s in chan.stages):
        return False
    only = case.get("only")
    return not only or any(key in name for key in only)


def _run_c_once(binary, file, chan, when, case, workdir):
    year, jday = when.year, when.timetuple().tm_yday
    mn, mx, n, spacing = case["freqs"]
    cmd = [binary, chan.station, chan.channel, str(year), str(jday), repr(mn), repr(mx), str(n),
           "-f", str(file), "-u", case["unit"], "-s", spacing, "-r", "cs",
           "-t", when.strftime("%H:%M:%S"), "-n", chan.network or "*", "-l", chan.location or "??"]
    if "b62_x" in case:
        cmd += ["-b62_x", repr(case["b62_x"])]
    if case.get("ts"):
        cmd.append("-ts")
    if case.get("estdelay"):
        cmd.append("-use-estimated-delay")
    if case.get("il"):
        cmd.append("-il")
    if case.get("stages"):
        first, last = case["stages"]
        cmd += ["-stage", str(first)] + ([str(last)] if last is not None else [])
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
    rows, err, rc = _run_c_once(binary, source.input_file(workdir), chan, when, case, workdir)
    if rows is None and (rc < 0 or rc >= 128) and source.blocks is not None:
        rows, err, rc = _run_c_once(binary, source.block_file(index, workdir), chan, when, case, workdir)
    return rows, err


class Source:
    """A response file prepared for comparison: its channels and the file C reads.

    ``blocks`` holds the per-channel RESP blocks, or None for StationXML read directly.
    """

    def __init__(self, path):
        self.path = Path(path).resolve()
        data = self.path.read_bytes()
        if sr.looks_like_stationxml(data[:1024]):
            self.text = None
            self.blocks = None
            self.channels = sr.parse_stationxml(data)
            return
        self.text = data.decode("utf-8", errors="replace")
        self.blocks = re.split(r"(?m)^(?=B050F03)", self.text)[1:]
        self.channels = sr.parse_resp(self.text, self.path.name)
        assert len(self.blocks) == len(self.channels)

    def input_file(self, workdir):
        """The whole response as a file for C (RESP is normalized to UTF-8 text)."""
        if self.text is None:
            return self.path
        whole = Path(workdir) / "whole.resp"
        whole.write_text(self.text)
        return whole

    def block_file(self, index, workdir):
        block = Path(workdir) / "block.resp"
        block.write_text(self.blocks[index])
        return block


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
    if stages and stages[1] is None:  # -stage N alone selects only stage N
        stages = (stages[0], stages[0])
    res = sr.evaluate(chan, freqs, unit=case["unit"], stages=stages,
                      use_total_sensitivity=case.get("ts", False),
                      use_estimated_delay=case.get("estdelay", False),
                      b62_x=case.get("b62_x", 0.0), interpolate_list=case.get("il", False))
    return [(f, v.real, v.imag) for f, v in zip(res.frequencies, res.values)], ""


def close(a, b, rtol):
    scale = max(abs(a), abs(b))
    return abs(a - b) <= rtol * scale + 1e-300


def _nyquist(chan, stages):
    """Nyquist frequency of the channel's output over the selected stage range, or None
    when no stage in it decimates.  ``stages`` is (first, last or None), as in CASES."""
    first, last = stages or (None, None)
    rates = [
        st.decimation.input_sample_rate / st.decimation.factor
        for st in chan.stages
        if st.sequence != 0 and st.decimation is not None
        and st.decimation.input_sample_rate > 0 and st.decimation.factor > 0
        and (first is None or first <= st.sequence <= (first if last is None else last))
    ]
    return min(rates) / 2 if rates else None


def compare(rows_c, rows_p, rtol, floor=1e-9, noise=1e-14, nyquist=None, rtol_above_nyquist=None):
    """Compare complex spectra.

    Error at each point is relative to that point's magnitude, after allowing an
    absolute difference of ``noise`` times the peak magnitude (cancellation near a
    stopband).  A point where both values are below ``floor`` times the peak (e.g. a
    FIR response above Nyquist, or an evaluation point on a zero of a digital filter)
    is numerical noise and legitimately differs between implementations.  A point above
    ``nyquist`` is aliased and ill-conditioned, so it is held to ``rtol_above_nyquist``.
    """
    if len(rows_c) != len(rows_p):
        return f"row count differs: C={len(rows_c)} py={len(rows_p)}"
    peak = max((abs(complex(r, i)) for _, r, i in rows_c if math.isfinite(r) and math.isfinite(i)), default=0.0) or 1.0
    worst = None
    for (fc, rc, ic), (fp, rp, ip) in zip(rows_c, rows_p):
        if not close(fc, fp, 1e-5):
            return f"frequency differs {fc} vs {fp}"
        ref, val = complex(rc, ic), complex(rp, ip)
        if ref == val:  # includes equal infinities
            continue
        if not (cmath.isfinite(ref) and cmath.isfinite(val)):
            return f"non-finite value differs at {fc}: {ref} vs {val}"
        mag = max(abs(ref), abs(val))
        if mag < floor * peak:
            continue
        err = max(abs(ref - val) - noise * peak, 0.0) / mag
        tol = rtol_above_nyquist if nyquist is not None and rtol_above_nyquist is not None and fc > nyquist else rtol
        if err > tol and (worst is None or err * worst[1] > worst[0] * tol):
            worst = (err, tol, fc)
    return None if worst is None else f"max relative error {worst[0]:.3e} > {worst[1]} at {worst[2]:g} Hz"


_SOURCES = {}


def _workdir(root):
    """A scratch directory private to this process, so concurrent tasks never share files."""
    path = Path(root) / str(os.getpid())
    path.mkdir(exist_ok=True)
    return str(path)


def _expand(paths):
    """Files named by paths; a directory contributes every file beneath it, in sorted order."""
    for path in paths:
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if not name.startswith("."):
                        yield os.path.join(dirpath, name)
        else:
            yield path


def _source(file):
    """The Source for a file; the most recent one is kept for consecutive tasks on the same file."""
    key = str(file)
    if key not in _SOURCES:
        _SOURCES.clear()
        _SOURCES[key] = Source(file)
    return _SOURCES[key]


def _check_epochs(task, stats, check):
    """Run ``check(source, index, workdir)`` on the channel epochs a task names (all with a
    start time when its indices are None); returns (stats, output lines).  A file that
    cannot be read is reported as skipped."""
    file, indices, root = task[:3]
    work = _workdir(root)
    try:
        source = _source(file)
    except SeismicResponseError as exc:
        return stats, [f"SKIP {file}: {exc}"]
    if indices is None:
        indices = [i for i, chan in enumerate(source.channels) if chan.start_time is not None]
    lines = []
    for index in indices:
        delta, out = check(source, index, work)
        for key, value in delta.items():
            stats[key] += value
        lines += out
    return stats, lines


def _file_tasks(files, jobs, root, extra):
    """One task per file; with fewer files than jobs, one per channel epoch so every job has work."""
    split = jobs > 1 and len(files) < jobs
    for file in files:
        indices = [None]
        if split:
            try:
                source = _source(file)
                indices = [[i] for i, chan in enumerate(source.channels) if chan.start_time is not None]
            except SeismicResponseError:
                pass  # the task reports the skip
        for index in indices:
            yield (file, index, root) + extra


def _epoch_time(chan):
    """A time inside the epoch; capped so open-ended epochs stay near the start
    (C evalresp produces nothing for dates centuries in the future)."""
    end = chan.end_time or (chan.start_time + timedelta(days=400))
    half = (end - chan.start_time) / 2
    return (chan.start_time + min(half, timedelta(days=60))).replace(microsecond=0)


def check_file(task):
    """Compare all CASES for the channel epochs of a task; returns (stats, output lines)."""
    binary, rtol, rtol_above, verbose = task[3:]
    stats = dict(ok=0, bad=0, both_error=0, only_c_error=0, only_py_error=0, deliberate=0)
    return _check_epochs(task, stats, lambda source, index, work:
                         _check_channel(source, index, work, binary, rtol, rtol_above, verbose))


def _check_channel(source, index, work, binary, rtol, rtol_above, verbose):
    """Compare all CASES for one channel epoch; returns (stats, output lines)."""
    file = source.path
    channels = source.channels
    chan = channels[index]
    when = _epoch_time(chan)
    stats = dict(ok=0, bad=0, both_error=0, only_c_error=0, only_py_error=0, deliberate=0)
    lines = []
    for case in CASES:
        if not applies(case, chan, Path(file).name):
            continue
        label = f"{Path(file).name} {chan.nslc} {when:%Y-%m-%d} {case}"
        rows_c, err_c = run_c(binary, source, index, chan, when, case, work)
        try:
            rows_p, err_p = run_py(channels, chan, when, case)
        except SeismicResponseError as exc:
            rows_p, err_p = None, str(exc)
        if rows_c is None and rows_p is None:
            stats["both_error"] += 1
            if verbose:
                lines.append(f"both error  {label}\n   C : {err_c[:150]}\n   py: {err_p[:150]}")
        elif rows_c is None:
            stats["only_c_error"] += 1
            lines.append(f"C ERROR ONLY {label}\n   C : {err_c[:200]}")
        elif rows_p is None and "not ground motion" in err_p:
            # evalresp returns such responses unconverted; refusing them is deliberate
            stats["deliberate"] += 1
            if verbose:
                lines.append(f"deliberate  {label}\n   py: {err_p[:200]}")
        elif rows_p is None:
            stats["only_py_error"] += 1
            lines.append(f"PY ERROR ONLY {label}\n   py: {err_p[:200]}")
        else:
            msg = compare(rows_c, rows_p, rtol, nyquist=_nyquist(chan, case.get("stages")),
                          rtol_above_nyquist=rtol_above)
            if msg:
                stats["bad"] += 1
                lines.append(f"MISMATCH {label}\n   {msg}")
            else:
                stats["ok"] += 1
    return stats, lines


def _ignore_sigint():
    """Worker initializer: only the parent handles Ctrl-C."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _run_tasks(fn, tasks, jobs, stats):
    """Run tasks (in parallel when jobs > 1); print each task's lines in task order
    and add its counts to ``stats``.  Pending tasks are dropped on interrupt;
    running ones finish."""
    def collect(results):
        for delta, lines in results:
            for line in lines:
                print(line, flush=True)
            for key, value in delta.items():
                stats[key] += value

    if jobs == 1:
        collect(map(fn, tasks))
    else:
        pool = ProcessPoolExecutor(max_workers=jobs, initializer=_ignore_sigint)
        try:
            collect(pool.map(fn, tasks))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("files", nargs="+", metavar="PATH", help="response file, or directory searched recursively")
    ap.add_argument("--rtol", type=float, default=2e-6)
    ap.add_argument("--rtol-above-nyquist", type=float, default=1e-4,
                    help="tolerance for frequencies above the channel's Nyquist frequency")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--formats", action="store_true", help="compare FAP/AP text output instead of complex spectra")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="number of comparisons to run in parallel")
    args = ap.parse_args()
    if args.jobs < 1:
        ap.error("--jobs must be at least 1")
    if not (os.path.isfile(args.binary) and os.access(args.binary, os.X_OK)):
        ap.error(f"evalresp binary not found or not executable: {args.binary}")
    files = list(_expand(args.files))
    if args.formats:
        return format_main(args.binary, files, args.jobs)

    stats = dict(ok=0, bad=0, both_error=0, only_c_error=0, only_py_error=0, deliberate=0)
    with tempfile.TemporaryDirectory() as root:
        tasks = _file_tasks(files, args.jobs, root, (args.binary, args.rtol, args.rtol_above_nyquist, args.v))
        _run_tasks(check_file, tasks, args.jobs, stats)
    print(stats)
    return 1 if stats["bad"] or stats["only_py_error"] else 0



# --------------------------------------------------------------------------- text formats
#
# The complex-spectra comparison above validates the numbers.  The FAP and AP
# text outputs add amplitude/phase conversion and phase unwrapping, checked here.

FORMAT_CASES = [
    dict(fmt="fap", extra=[], unwrap=True, freqs=(0.001, 40, 40, "log")),
    dict(fmt="ap", extra=[], unwrap=False, freqs=(0.001, 40, 40, "log")),
    dict(fmt="ap", extra=["-unwrap"], unwrap=True, freqs=(0.001, 40, 40, "log")),
    dict(fmt="fap", extra=[], unwrap=True, freqs=(0.01, 20, 30, "lin")),
]


def run_c_format(binary, file, chan, when, case, workdir):
    from pathlib import Path as _P
    mn, mx, n, spacing = case["freqs"]
    for old in _P(workdir).glob("[AFPS]*.*"):
        old.unlink()
    cmd = [binary, chan.station, chan.channel, str(when.year), str(when.timetuple().tm_yday), repr(mn), repr(mx),
           str(n), "-f", str(file), "-u", "def", "-s", spacing, "-r", case["fmt"],
           "-t", when.strftime("%H:%M:%S"), "-n", chan.network or "*", "-l", chan.location or "??"] + case["extra"]
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
                      unit="def")
    ref = run_c_format(binary, source.input_file(workdir), chan, when, case, workdir)
    if ref is None and source.blocks is not None:
        ref = run_c_format(binary, source.block_file(index, workdir), chan, when, case, workdir)
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


def check_file_formats(task):
    """Compare all FORMAT_CASES for the channel epochs of a task; returns (stats, output lines)."""
    binary = task[3]
    return _check_epochs(task, dict(ok=0, bad=0, skipped=0), lambda source, index, work:
                         _check_channel_formats(source, index, work, binary))


def _check_channel_formats(source, index, work, binary):
    """Compare all FORMAT_CASES for one channel epoch; returns (stats, output lines)."""
    file = source.path
    chan = source.channels[index]
    when = _epoch_time(chan)
    stats = dict(ok=0, bad=0, skipped=0)
    lines = []
    for case in FORMAT_CASES:
        try:
            msg = compare_formats(binary, source, index, chan, when, case, work)
        except SeismicResponseError:
            msg = "skip"
        if msg == "skip":
            stats["skipped"] += 1
        elif msg:
            stats["bad"] += 1
            lines.append(f"FORMAT MISMATCH {Path(file).name} {chan.nslc} {when:%Y-%m-%d} {case['fmt']} "
                         f"{case['extra']}: {msg}")
        else:
            stats["ok"] += 1
    return stats, lines


def format_main(binary, files, jobs=1):
    stats = dict(ok=0, bad=0, skipped=0)
    with tempfile.TemporaryDirectory() as root:
        tasks = _file_tasks(files, jobs, root, (binary,))
        _run_tasks(check_file_formats, tasks, jobs, stats)
    print(stats)
    return 1 if stats["bad"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)

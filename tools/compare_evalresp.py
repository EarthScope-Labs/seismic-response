#!/usr/bin/env python3
"""Compare seismic_response against a built C evalresp binary.

Runs the C program and this package on the same inputs across a matrix of
options and reports any differences beyond print precision.  Developer tool:

    python tools/compare_evalresp.py [--jobs N] /path/to/evalresp/src/evalresp FILE [FILE ...]

Each channel epoch of each file is tested at a time inside the epoch.  StationXML
files are given to evalresp as they are; it detects the format itself.

With --jobs N, N channel epochs are compared in parallel; output order and
results are the same as for a serial run.

With --via-xml2resp, StationXML is first converted by xml2resp and both sides
evaluate that RESP.  This compares evaluation alone: the conversion rounds delays
to 5 and coefficients to 6 significant digits, which a direct XML read does not.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

import seismic_response as sr
from seismic_response.errors import SeismicResponseError

CASES = [
    dict(freqs=(0.001, 40, 25, "log")),
    dict(freqs=(0.01, 20, 17, "lin")),
    dict(freqs=(0.005, 15, 21, "log")),
    dict(freqs=(0.02, 0.02, 1, "log")),
]


def _run_c_once(binary, file, chan, when, case, workdir):
    year, jday = when.year, when.timetuple().tm_yday
    mn, mx, n, spacing = case["freqs"]
    cmd = [binary, chan.station, chan.channel, str(year), str(jday), repr(mn), repr(mx), str(n),
           "-f", str(file), "-u", "def", "-s", spacing, "-r", "cs",
           "-t", when.strftime("%H:%M:%S"), "-n", chan.network or "*", "-l", chan.location or "??"]
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
    With ``xml2resp`` (a converter executable), StationXML is converted to RESP in
    ``workdir`` first and handled as RESP.
    """

    def __init__(self, path, xml2resp=None, workdir=None):
        self.path = Path(path).resolve()
        data = self.path.read_bytes()
        if sr.looks_like_stationxml(data[:1024]):
            if xml2resp is None:
                self.text = None
                self.blocks = None
                self.channels = sr.parse_stationxml(data)
                return
            data = self._convert(xml2resp, workdir)
        self.text = data.decode("utf-8", errors="replace")
        self.blocks = re.split(r"(?m)^(?=B050F03)", self.text)[1:]
        self.channels = sr.parse_resp(self.text, self.path.name)
        assert len(self.blocks) == len(self.channels)

    def _convert(self, xml2resp, workdir):
        out = Path(workdir) / "xml2resp.resp"
        if out.exists():
            out.unlink()
        p = subprocess.run([xml2resp, "-o", str(out), str(self.path)], capture_output=True, text=True)
        if p.returncode != 0 or not out.exists():
            raise SeismicResponseError(f"xml2resp failed: {p.stderr.strip()[-150:] or f'rc={p.returncode}'}")
        return out.read_bytes()

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
    res = sr.evaluate(chan, freqs, unit="def")
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


_SOURCES = {}


def _workdir(root):
    """A scratch directory private to this process, so concurrent tasks never share files."""
    path = Path(root) / str(os.getpid())
    path.mkdir(exist_ok=True)
    return str(path)


def _source(file, xml2resp, workdir):
    """The Source for a file, prepared once per process."""
    key = (str(file), xml2resp)
    if key not in _SOURCES:
        _SOURCES[key] = Source(file, xml2resp=xml2resp, workdir=workdir)
    return _SOURCES[key]


def _epoch_time(chan):
    """A time inside the epoch; capped so open-ended epochs stay near the start
    (C evalresp produces nothing for dates centuries in the future)."""
    end = chan.end_time or (chan.start_time + timedelta(days=400))
    half = (end - chan.start_time) / 2
    return (chan.start_time + min(half, timedelta(days=60))).replace(microsecond=0)


def check_channel(task):
    """Compare all CASES for one channel epoch; returns (stats, output lines)."""
    file, index, xml2resp, root, binary, rtol, verbose = task
    work = _workdir(root)
    source = _source(file, xml2resp, work)
    channels = source.channels
    chan = channels[index]
    when = _epoch_time(chan)
    stats = dict(ok=0, bad=0, both_error=0, only_c_error=0, only_py_error=0)
    lines = []
    for case in CASES:
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
        elif rows_p is None:
            stats["only_py_error"] += 1
            lines.append(f"PY ERROR ONLY {label}\n   py: {err_p[:200]}")
        else:
            msg = compare(rows_c, rows_p, rtol)
            if msg:
                stats["bad"] += 1
                lines.append(f"MISMATCH {label}\n   {msg}")
            else:
                stats["ok"] += 1
    return stats, lines


def _run_tasks(fn, tasks, jobs, stats):
    """Run tasks (in parallel when jobs > 1); print each task's lines in task order
    and add its counts to ``stats``."""
    def collect(results):
        for delta, lines in results:
            for line in lines:
                print(line, flush=True)
            for key, value in delta.items():
                stats[key] += value

    if jobs == 1:
        collect(map(fn, tasks))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            collect(pool.map(fn, tasks))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--rtol", type=float, default=2e-6)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--formats", action="store_true", help="compare FAP/AP text output instead of complex spectra")
    ap.add_argument("--via-xml2resp", action="store_true",
                    help="convert StationXML with xml2resp and evaluate that RESP on both sides")
    ap.add_argument("--xml2resp", metavar="PATH",
                    help="xml2resp executable for --via-xml2resp (default: next to the evalresp binary)")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="number of comparisons to run in parallel")
    args = ap.parse_args()
    if args.jobs < 1:
        ap.error("--jobs must be at least 1")
    if not (os.path.isfile(args.binary) and os.access(args.binary, os.X_OK)):
        ap.error(f"evalresp binary not found or not executable: {args.binary}")
    xml2resp = None
    if args.via_xml2resp:
        xml2resp = args.xml2resp or str(Path(args.binary).parent / "xml2resp")
        if not (os.path.isfile(xml2resp) and os.access(xml2resp, os.X_OK)):
            ap.error(f"xml2resp not found or not executable: {xml2resp}")
    if args.formats:
        return format_main(args.binary, args.files, xml2resp, args.jobs)

    stats = dict(ok=0, bad=0, both_error=0, only_c_error=0, only_py_error=0)
    with tempfile.TemporaryDirectory() as root:
        tasks = []
        for file in args.files:
            try:
                source = _source(file, xml2resp, _workdir(root))
            except SeismicResponseError as exc:
                print(f"SKIP {file}: {exc}")
                continue
            for index, chan in enumerate(source.channels):
                if chan.start_time is not None:
                    tasks.append((file, index, xml2resp, root, args.binary, args.rtol, args.v))
        _run_tasks(check_channel, tasks, args.jobs, stats)
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


def check_channel_formats(task):
    """Compare all FORMAT_CASES for one channel epoch; returns (stats, output lines)."""
    file, index, xml2resp, root, binary = task
    work = _workdir(root)
    source = _source(file, xml2resp, work)
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


def format_main(binary, files, xml2resp=None, jobs=1):
    stats = dict(ok=0, bad=0, skipped=0)
    with tempfile.TemporaryDirectory() as root:
        tasks = []
        for file in files:
            source = _source(file, xml2resp, _workdir(root))
            for index, chan in enumerate(source.channels):
                if chan.start_time is not None:
                    tasks.append((file, index, xml2resp, root, binary))
        _run_tasks(check_channel_formats, tasks, jobs, stats)
    print(stats)
    return 1 if stats["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Command line interfaces.

``pyevalresp`` is a drop-in for the C ``evalresp`` command line::

    pyevalresp STALST CHALST YYYY DAY MINFREQ MAXFREQ NFREQ [options]

``seismic-response`` is the umbrella command with sub-commands::

    seismic-response evalresp ...   # same as pyevalresp
    seismic-response info FILE      # list channels and stages in a response file
    seismic-response gui            # open the interactive plotting GUI
"""

from __future__ import annotations

import argparse
import glob
import itertools
import logging
import os
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from . import __version__, parse_response
from .errors import ResponseParseError, ResponseValueError, SeismicResponseError, UnsupportedFeatureError
from .evaluate import evaluate, generate_frequencies
from .model import Channel
from .output import describe, render
from .selection import _select, split_patterns

# Exit codes follow evalresp's error enumeration
EXIT_OK = 0
EXIT_IO = 2
EXIT_INPUT = 3
EXIT_PARSE = 5
EXIT_VALIDATION = 7

USAGE = """
EVALRESP (seismic-response {version})

USAGE: {prog} STALST CHALST YYYY DAY MINFREQ MAXFREQ NFREQ [options]

  OPTIONS:

    -f file              (directory-name|filename)
    -u units             ('def'|'dis'|'vel'|'acc'; default 'def', evalresp: 'vel')
    -t time-of-day       (HH:MM:SS)
    -s type-of-spacing   (log|lin)
    -n netid             ('II'|'IU'|'G'|'*'...)
    -l locid             ('01'|'AA,AB,AC'|'A?'|'*'...)
    -r resp_type         ('ap'=amp/pha | 'cs'=complex spectra |
                          'fap'=freq/amp/pha)
    -stage start [stop]  (start and stop are integer stage numbers)
    -stdio               (take input from stdin, output to stdout)
    -use-estimated-delay (use estimated delay instead of correction applied
                          in computation of ASYM FIR response)
    -il                  (interpolate List blockette output)
    -ii                  (same as -il)
    -unwrap              (unwrap phase if the output is AP)
    -ts                  (use total sensitivity from stage 0 instead of
                          computed)
    -b62_x value         (sample value/volts where we compute response for
                          B62)
    -v                   (verbose; list parameters on stderr)
    -x                   (expect FDSN StationXML format, default autodetect)

  NOTES:

    (1) If the 'file' argument is a directory, that directory will be
        searched for files of the form RESP.NETID.STA.LOC.CHA
    (2) If the 'file' argument is a file, the format of the file is
        autodetected to be StationXML, SAC poles-and-zeros or RESP,
        defaulting to RESP.
        The expected format can be forced to StationXML using '-x'.
    (3) If the 'file' argument is missing, the current directory
        will be searched for files of the form RESP.NETID.STA.LOC.CHA.
    (4) The directory indicated by the environment variable SEEDRESP
        will also be searched for the requested files (if it is defined).
    (5) The NETID indicates a one or two letter network code.  If no network
        code exists in the SEED volume, a value of '', '??', or '*' will
        match the 'no network' case.
    (6) The '-stage' option allows the user to specify the range of
        stages that will be included in the calculation.  If only one
        stage number is given, a response will only be calculated for
        that stage.  If both a start and stop stage number are given,
        any stage between (and including) the start and stop stages
        will be included in the calculation.
    (7) -b62_x defines a value in counts or volts where response is
        computed. This flag only is applied to responses with B62.

  EXAMPLES:

    {prog} AAK,ARU,TLY VHZ 1992 21 0.001 10 100 -f /EVRESP/NEW/rdseed.out
    {prog} KONO BHN,BHE 1992 1 0.001 10 100 -f /EVRESP/NEW -t 12:31:04 -v
    {prog} FRB BHE,BHZ 1994 31 0.001 10 100 -f resp.all_stations -n '*' -v
"""


class _InputError(Exception):
    """Bad command line input."""


class _EvalrespFormatter(logging.Formatter):
    """Log lines like evalresp's: ``Thu Nov  5 15:22:38 2026 [INFO] message``."""

    def format(self, record: logging.LogRecord) -> str:
        # "%e" (space-padded day) is not supported by strftime on Windows, so pad by hand
        t = _time.localtime(record.created)
        stamp = _time.strftime("%a %b ", t) + f"{t.tm_mday:2d}" + _time.strftime(" %H:%M:%S %Y", t)
        level = {"WARNING": "WARN", "CRITICAL": "ERROR"}.get(record.levelname, record.levelname)
        return f"{stamp} [{level}] {record.getMessage()}"


def _setup_logging(verbose: int) -> None:
    log = logging.getLogger("seismic_response")
    for h in list(log.handlers):
        log.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_EvalrespFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO if verbose else logging.WARNING)
    log.propagate = False


# ------------------------------------------------------------------ argument parsing


def _is_real(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


def _is_int(s: str) -> bool:
    try:
        int(s)
    except ValueError:
        return False
    return True


class _Args:
    def __init__(self) -> None:
        self.stations = ""
        self.channels = ""
        self.year = 0
        self.jday = 0
        self.minfreq = 1.0
        self.maxfreq: Optional[float] = None
        self.nfreq = 1
        self.file: Optional[str] = None
        self.unit = "def"
        self.time = (0, 0, 0.0)
        self.spacing = "log"
        self.network: Optional[str] = None
        self.location: Optional[str] = None
        self.format: Optional[str] = None
        self.start_stage: Optional[int] = None
        self.stop_stage: Optional[int] = None
        self.stdio = False
        self.estimated_delay = False
        self.interpolate = False
        self.unwrap = False
        self.total_sensitivity = False
        self.b62_x = 0.0
        self.verbose = 0
        self.xml = False


_VALUE_OPTIONS = {
    "-f": "file", "--file": "file",
    "-u": "unit", "--units": "unit",
    "-t": "time", "--time": "time",
    "-s": "spacing", "--spacing": "spacing",
    "-n": "network", "--network": "network",
    "-l": "location", "--location": "location",
    "-r": "format", "--response": "format",
    "-b62_x": "b62_x", "--b62_x": "b62_x",
}
_FLAG_OPTIONS = {
    "-stdio": "stdio", "--stdio": "stdio",
    "-use-estimated-delay": "estimated_delay", "--use-estimated-delay": "estimated_delay",
    "-use-delay": "estimated_delay", "--use-delay": "estimated_delay",
    "-il": "interpolate", "--il": "interpolate",
    "-ii": "interpolate", "--ii": "interpolate",
    "-unwrap": "unwrap", "--unwrap": "unwrap",
    "-ts": "total_sensitivity", "--ts": "total_sensitivity",
    "-x": "xml", "--xml": "xml",
}


def _parse_time_of_day(text: str) -> Tuple[int, int, float]:
    """``hh[:mm[:ss[.sss]]]`` as accepted by evalresp."""
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = float(parts[2]) if len(parts) > 2 else 0.0
        if len(parts) > 3:
            raise ValueError
    except ValueError:
        raise _InputError(f"Cannot parse '{text}' as a time") from None
    return hour, minute, second


def parse_evalresp_args(argv: Sequence[str]) -> _Args:
    """Parse an evalresp style command line (``argv`` without the program name)."""
    a = _Args()
    tokens = list(argv)

    # Leading positionals run until the first option (negative numbers are not options)
    n_pos = 0
    while n_pos < len(tokens) and (not tokens[n_pos].startswith("-") or _is_real(tokens[n_pos])):
        n_pos += 1
    positionals, rest = tokens[:n_pos], tokens[n_pos:]

    if len(positionals) < 4:
        raise _InputError(
            f"Not all of the required inputs are present ({4 - len(positionals)} missing)"
        )
    a.stations, a.channels = positionals[0], positionals[1]
    try:
        a.year = int(positionals[2])
        a.jday = int(positionals[3])
        a.minfreq = float(positionals[4]) if len(positionals) > 4 else 1.0
        a.maxfreq = float(positionals[5]) if len(positionals) > 5 else a.minfreq
        a.nfreq = int(positionals[6]) if len(positionals) > 6 else 1
    except ValueError as exc:
        raise _InputError(f"Cannot parse numeric argument: {exc}") from None
    for extra in positionals[7:]:
        logging.getLogger("seismic_response").warning("Unrecognized parameter:  %s", extra)

    i = 0
    while i < len(rest):
        tok = rest[i]
        i += 1
        if tok in ("-v", "--verbose"):
            a.verbose += 1
        elif tok in ("-vv",):
            a.verbose += 2
        elif tok in ("-stage", "--stage"):
            if i >= len(rest) or not _is_int(rest[i]):
                raise _InputError("Missing argument for option 'stage'")
            a.start_stage = int(rest[i])
            i += 1
            if i < len(rest) and _is_int(rest[i]):
                a.stop_stage = int(rest[i])
                i += 1
        elif tok in _FLAG_OPTIONS:
            setattr(a, _FLAG_OPTIONS[tok], True)
        elif tok in _VALUE_OPTIONS:
            if i >= len(rest):
                raise _InputError(f"Missing argument for option '{tok.lstrip('-')}'")
            name, value = _VALUE_OPTIONS[tok], rest[i]
            i += 1
            if name == "time":
                a.time = _parse_time_of_day(value)
            elif name == "b62_x":
                try:
                    a.b62_x = float(value)
                except ValueError:
                    raise _InputError(f"Cannot parse block 62 x value '{value}'") from None
            elif name == "unit":
                if value.lower() not in ("def", "dis", "vel", "acc"):
                    raise _InputError(f"Could not match '{value}' for output unit")
                a.unit = value.lower()
            elif name == "spacing":
                if value.lower() not in ("log", "lin"):
                    raise _InputError(f"Could not match '{value}' for frequency spacing")
                a.spacing = value.lower()
            elif name == "format":
                if value.lower() not in ("ap", "fap", "cs"):
                    raise _InputError(f"Could not match '{value}' for file format")
                a.format = value.lower()
            else:
                setattr(a, name, value)
        else:
            logging.getLogger("seismic_response").warning("Unrecognized option: %s", tok)
    return a


# ------------------------------------------------------------------ file handling


def _read_channels(data: bytes, origin: str, force_xml: bool) -> List[Channel]:
    return parse_response(data, origin, fmt="stationxml" if force_xml else "auto")


def _find_files(file_arg: Optional[str], sncls) -> List[str]:
    """Locate input files like evalresp: a file, a directory of RESP.* files, or the cwd."""
    if file_arg:
        if os.path.isdir(file_arg):
            dirs = [file_arg]
        else:
            return [file_arg]
    else:
        dirs = ["."]
        seedresp = os.environ.get("SEEDRESP")
        if seedresp:
            dirs.append(seedresp)

    found: List[str] = []
    for net, sta, loc, cha in sncls:
        loc_wild = loc in ("*", "??")
        for d in dirs:
            names = sorted(glob.glob(os.path.join(d, f"RESP.{net}.{sta}.{'*' if loc_wild else loc}.{cha}")))
            if not names and loc_wild:
                names = sorted(glob.glob(os.path.join(d, f"RESP.{net}.{sta}.{cha}")))
            if not names:
                logging.getLogger("seismic_response").warning(
                    "no files match '%s'", os.path.join(d, f"RESP.{net}.{sta}.{loc}.{cha}")
                )
            for n in names:
                if n not in found:
                    found.append(n)
    return found


def _build_sncls(a: _Args):
    stations = split_patterns(a.stations)
    channels = split_patterns(a.channels)
    if a.location is None:
        locations = ["*"]
    else:
        locations = a.location.split(",") if a.location != "" else [""]
    net = a.network if a.network is not None else "*"
    return [(net, sta, loc, cha) for sta, loc, cha in itertools.product(stations, locations, channels)]


# ------------------------------------------------------------------ main entry points


def evalresp_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``pyevalresp`` (evalresp compatible command line)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "pyevalresp"
    if prog in ("__main__.py", "-c", "") or prog.endswith(".py"):
        prog = "pyevalresp"
    _setup_logging(0)
    log = logging.getLogger("seismic_response")

    try:
        a = parse_evalresp_args(argv)
    except _InputError as exc:
        log.error("%s", exc)
        print(USAGE.format(version=__version__, prog=prog), file=sys.stderr)
        return EXIT_INPUT

    _setup_logging(a.verbose)
    if a.verbose:
        log.info("<< EVALRESP RESPONSE OUTPUT (seismic-response %s) >>", __version__)

    try:
        return _run_evalresp(a)
    except _InputError as exc:
        log.error("%s", exc)
        return EXIT_INPUT
    except UnsupportedFeatureError as exc:
        log.error("%s", exc)
        return EXIT_INPUT
    except ResponseParseError as exc:
        log.error("%s", exc)
        return EXIT_PARSE
    except ResponseValueError as exc:
        log.error("%s", exc)
        return EXIT_VALIDATION
    except OSError as exc:
        log.error("%s", exc)
        return EXIT_IO
    except (SeismicResponseError, ValueError) as exc:
        log.error("%s", exc)
        return EXIT_INPUT


def _exit_code(exc: Exception) -> int:
    if isinstance(exc, UnsupportedFeatureError):
        return EXIT_INPUT
    if isinstance(exc, ResponseParseError):
        return EXIT_PARSE
    if isinstance(exc, ResponseValueError):
        return EXIT_VALIDATION
    return EXIT_INPUT


def _run_evalresp(a: _Args) -> int:
    log = logging.getLogger("seismic_response")
    hour, minute, second = a.time
    try:
        when = datetime(a.year, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=a.jday - 1, hours=hour, minutes=minute, seconds=second
        )
    except (ValueError, OverflowError):
        raise _InputError(f"Invalid year/day: {a.year} {a.jday}") from None

    freqs = generate_frequencies(a.minfreq, a.maxfreq, a.nfreq, linear=a.spacing == "lin")
    sncls = _build_sncls(a)
    fmt = a.format or ("fap" if a.stdio else "ap")

    sources: List[Tuple[str, bytes]] = []
    if a.stdio:
        if a.file:
            log.warning("Using stdio so ignoring file '%s'", a.file)
        sources.append(("stdin", sys.stdin.buffer.read()))
    else:
        for name in _find_files(a.file, sncls):
            try:
                with open(name, "rb") as fp:
                    sources.append((name, fp.read()))
            except OSError:
                log.error("Cannot open %s", name)
                return EXIT_IO

    stages = None
    if a.start_stage is not None:
        # evalresp: a lone start stage selects just that stage
        stages = (a.start_stage, a.stop_stage if a.stop_stage else a.start_stage)

    results = []
    matched = False
    status = EXIT_OK
    for name, data in sources:
        try:
            channels = _read_channels(data, os.path.basename(name), a.xml)
        except SeismicResponseError as exc:
            log.error("%s", exc)
            status = status or _exit_code(exc)
            continue
        for chan in _select(channels, sncls, None, when):
            matched = True
            try:
                res = evaluate(
                    chan,
                    freqs,
                    unit=a.unit,
                    stages=stages,
                    use_total_sensitivity=a.total_sensitivity,
                    use_estimated_delay=a.estimated_delay,
                    b62_x=a.b62_x,
                    interpolate_list=a.interpolate,
                )
            except SeismicResponseError as exc:  # report and carry on with the next channel
                log.error("%s", exc)
                status = status or _exit_code(exc)
                continue
            results.append((name, res))

    if not matched:
        log.warning("No channels matched the requested stations, channels and time")

    for name, res in results:
        if a.verbose:
            for line in describe(res, source=name if not a.stdio else ""):
                log.info("%s", line)

    # Traditionally FAP is always unwrapped; AP only on request
    for _, res in results:
        for filename, text in render(res, fmt, unwrap=a.unwrap, stdio=a.stdio):
            if a.stdio:
                sep = " " + "-" * 50 + "\n"
                sys.stdout.write(f"{sep} {filename}\n{sep}{text}{sep}")
            else:
                with open(filename, "w", encoding="utf-8", newline="\n") as fp:
                    fp.write(text)
    return status


# ------------------------------------------------------------------ umbrella command


def _cmd_info(args: argparse.Namespace) -> int:
    from . import read_response

    try:
        channels = read_response(args.file)
    except (OSError, SeismicResponseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO if isinstance(exc, OSError) else EXIT_PARSE

    for c in channels:
        print(c)
        if args.stages:
            for st in c.stages:
                label = "total sensitivity" if st.sequence == 0 else f"stage {st.sequence}"
                extra = ""
                if st.gain is not None:
                    extra = f", gain {st.gain.value:.6g} @ {st.gain.frequency:g} Hz"
                units = f"  [{st.input_units} -> {st.output_units}]" if st.input_units else ""
                print(f"    {label}: {st.filter_kind}{extra}{units}")
    return EXIT_OK


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui.server import serve

    return serve(host=args.host, port=args.port, open_browser=not args.no_browser, native=args.app, files=args.files)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``seismic-response``."""
    argv = list(sys.argv[1:] if argv is None else argv)

    # `seismic-response evalresp ...` passes everything through untouched so that
    # evalresp's single-dash options are not interpreted by argparse.
    if argv and argv[0] == "evalresp":
        return evalresp_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="seismic-response",
        description="Evaluate seismological instrument responses (StationXML, SEED RESP).",
        epilog="Use 'seismic-response evalresp -h'-style arguments or the 'pyevalresp' command "
        "for the evalresp compatible interface.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("evalresp", help="evalresp compatible command line (see pyevalresp)")

    p_info = sub.add_parser("info", help="list the channels (and stages) in a response file")
    p_info.add_argument("file")
    p_info.add_argument("-s", "--stages", action="store_true", help="also list each stage")
    p_info.set_defaults(func=_cmd_info)

    p_gui = sub.add_parser("gui", help="launch the interactive response plotting GUI")
    p_gui.add_argument("files", nargs="*", help="response files to load at startup")
    p_gui.add_argument("--host", default="127.0.0.1")
    p_gui.add_argument("--port", type=int, default=0, help="port to serve on (default: any free port)")
    p_gui.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p_gui.add_argument("--app", action="store_true", help="use a native desktop window (requires pywebview)")
    p_gui.set_defaults(func=_cmd_gui)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

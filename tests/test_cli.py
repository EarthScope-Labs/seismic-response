"""The evalresp compatible command line."""

import io
import sys
from pathlib import Path

import pytest
from conftest import DATA

from seismic_response import cli

ANMO = str(DATA / "real" / "RESP.IU.ANMO.00.BHZ")
BASE = ["ANMO", "BHZ", "2000", "100", "0.01", "10", "3"]


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    """Run evalresp_main in a scratch directory; returns (exit code, stdout, stderr)."""
    monkeypatch.chdir(tmp_path)

    def _run(*args, stdin=None):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
        rc = cli.evalresp_main(list(args))
        cap = capsys.readouterr()
        return rc, cap.out, cap.err

    _run.dir = tmp_path
    return _run


def rows(path):
    return [tuple(float(x) for x in line.split()) for line in Path(path).read_text().splitlines()]


def test_default_output_is_amplitude_and_phase_files(run):
    rc, out, err = run(*BASE, "-f", ANMO)
    assert rc == 0 and out == ""
    amp, phase = run.dir / "AMP.IU.ANMO.00.BHZ", run.dir / "PHASE.IU.ANMO.00.BHZ"
    assert amp.exists() and phase.exists()
    # Values reported by the C program for this response (see tests/data/golden)
    assert rows(amp) == [
        (pytest.approx(0.01, rel=1e-6), pytest.approx(650419300.0, rel=1e-6)),
        (pytest.approx(0.316228, rel=1e-6), pytest.approx(988160700.0, rel=1e-6)),
        (pytest.approx(10.0), pytest.approx(24021.24, rel=1e-6)),
    ]
    assert rows(phase)[0][1] == pytest.approx(53.53581, rel=1e-6)


def test_fap_and_complex_formats(run):
    assert run(*BASE, "-f", ANMO, "-r", "fap")[0] == 0
    assert rows(run.dir / "FAP.IU.ANMO.00.BHZ")[2][1:] == (pytest.approx(24021.24, rel=1e-6), pytest.approx(-179.9037, rel=1e-6))
    assert run(*BASE, "-f", ANMO, "-r", "cs")[0] == 0
    cs = rows(run.dir / "SPECTRA.IU.ANMO.00.BHZ")
    assert cs[0][1:] == (pytest.approx(386557400.0, rel=1e-6), pytest.approx(523085700.0, rel=1e-6))


def test_stdio_layout_matches_evalresp(run):
    rc, out, err = run(*BASE, "-stdio", stdin=Path(ANMO).read_bytes())
    assert rc == 0
    lines = out.splitlines()
    sep = " " + "-" * 50
    assert lines[:3] == [sep, " AMP/PHS.IU.ANMO.00.BHZ", sep]  # stdio defaults to FAP
    assert lines[-1] == sep
    assert len(lines) == 3 + 3 + 1
    assert lines[4].split()[1] == "9.881607E+08"


def test_options_unit_stage_and_spacing(run):
    run(*BASE, "-f", ANMO, "-r", "cs", "-u", "def", "-s", "lin", "-stage", "1", "1")
    cs = rows(run.dir / "SPECTRA.IU.ANMO.00.BHZ")
    assert [r[0] for r in cs] == pytest.approx([0.01, 5.005, 10.0])
    assert abs(complex(*cs[0][1:])) < 1e5  # stage 1 only: no digitizer gain


def test_time_selects_epoch_and_open_epoch_has_none_after_gap(run):
    # 1999 day 100 falls in the first epoch; 2001 in the second: same response here, both found
    assert run("ANMO", "BHZ", "1999", "100", "1", "1", "1", "-f", ANMO, "-r", "fap")[0] == 0
    assert (run.dir / "FAP.IU.ANMO.00.BHZ").exists()
    # before the first epoch nothing matches: evalresp is silent, this package warns
    rc, out, err = run("ANMO", "BHZ", "1990", "100", "1", "1", "1", "-f", ANMO, "-r", "fap")
    assert rc == 0 and "No channels matched" in err


def test_wildcards_network_and_location_filters(run):
    assert run("*", "B*", "2000", "100", "1", "1", "1", "-f", ANMO, "-n", "II", "-r", "fap")[0] == 0
    assert not list(run.dir.glob("FAP.*"))  # wrong network: nothing
    assert run("*", "B*", "2000", "100", "1", "1", "1", "-f", ANMO, "-n", "IU", "-l", "00,10", "-r", "fap")[0] == 0
    assert (run.dir / "FAP.IU.ANMO.00.BHZ").exists()


def test_directory_search_for_resp_files(run):
    (run.dir / "RESP.IU.ANMO.00.BHZ").write_bytes(Path(ANMO).read_bytes())
    assert run(*BASE, "-r", "fap")[0] == 0  # no -f: current directory
    assert run(*BASE, "-f", str(run.dir), "-r", "cs")[0] == 0  # directory given
    assert (run.dir / "SPECTRA.IU.ANMO.00.BHZ").exists()


def test_verbose_logs_to_stderr_not_stdout(run):
    rc, out, err = run(*BASE, "-f", ANMO, "-v", "-r", "fap")
    assert rc == 0 and out == ""
    assert "[INFO]" in err and "computed sens=" in err and "stage  1: LAPLACE" in err


def test_stationxml_input_with_x_flag_and_autodetect(run):
    xml = str(DATA / "real" / "station-3.xml")
    # discover the channel in the file
    import seismic_response as sr

    c = sr.read_response(xml)[0]
    when = c.start_time
    args = [c.station, c.channel, str(when.year), str(when.timetuple().tm_yday + 5), "1", "1", "1", "-f", xml, "-r", "fap"]
    assert run(*args)[0] == 0
    assert run(*args, "-x")[0] == 0
    assert list(run.dir.glob("FAP.*"))


@pytest.mark.parametrize(
    "args, code",
    [
        (["ANMO", "BHZ", "2000"], 3),  # too few arguments
        ([*BASE, "-f", ANMO, "-u", "furlongs"], 3),  # bad unit
        ([*BASE, "-f", ANMO, "-s", "sideways"], 3),
        ([*BASE, "-f", ANMO, "-r", "xml"], 3),
        ([*BASE, "-f", ANMO, "-t", "noon"], 3),
        ([*BASE, "-f", ANMO, "-il"], 3),  # unsupported here
        ([*BASE, "-f", "/no/such/file"], 2),  # I/O error
        (["ANMO", "BHZ", "2000", "100", "0", "10", "3", "-f", ANMO], 7),  # zero frequency on a log axis
    ],
)
def test_error_exit_codes(run, args, code):
    rc, out, err = run(*args)
    assert rc == code and "[ERROR]" in err


def test_usage_is_shown_for_bad_invocations(run):
    rc, out, err = run("ANMO")
    assert rc == 3 and "USAGE:" in err and "-stage start [stop]" in err


def test_parse_args_handles_negative_numbers_and_stage_ranges():
    a = cli.parse_evalresp_args(["S", "C", "2000", "1", "-1", "10", "5", "-stage", "2", "4", "-v", "-v", "-unwrap"])
    assert (a.minfreq, a.maxfreq, a.nfreq) == (-1.0, 10.0, 5)
    assert (a.start_stage, a.stop_stage, a.verbose, a.unwrap) == (2, 4, 2, True)
    b = cli.parse_evalresp_args(["S", "C", "2000", "1", "-stage", "3", "-u", "acc"])
    assert (b.start_stage, b.stop_stage, b.unit) == (3, None, "acc")
    assert b.maxfreq == b.minfreq == 1.0 and b.nfreq == 1  # evalresp defaults


def test_umbrella_command_info_and_evalresp_passthrough(run, capsys):
    assert cli.main(["info", ANMO, "--stages"]) == 0
    text = capsys.readouterr().out
    assert "FDSN:IU_ANMO_00_B_H_Z" in text and "stage 1: poles-zeros (Laplace, rad/s)" in text
    assert "total sensitivity" in text
    assert cli.main(["evalresp", *BASE, "-f", ANMO, "-r", "fap"]) == 0
    assert (run.dir / "FAP.IU.ANMO.00.BHZ").exists()
    assert cli.main([]) == 0  # no sub-command prints the help text


def test_python_dash_m_entry_point():
    import subprocess

    out = subprocess.run([sys.executable, "-m", "seismic_response", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("seismic-response ")
    info = subprocess.run([sys.executable, "-m", "seismic_response", "info", ANMO], capture_output=True, text=True)
    assert info.returncode == 0 and "FDSN:IU_ANMO_00_B_H_Z" in info.stdout

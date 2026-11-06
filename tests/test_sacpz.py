"""SAC poles-and-zeros (SAC PZ) parsing and evaluation."""

import json
import math
from datetime import datetime, timezone

import pytest
from conftest import DATA

import seismic_response as sr
from seismic_response import ResponseParseError, web_api
from seismic_response.sacpz import looks_like_sacpz, parse_sacpz

UTC = timezone.utc
PFO = DATA / "synthetic" / "sacpz_II_PFO_BHZ.pz"
CONSTANT = 3.816863e11
ZEROS = [-7.853982e01 + 0j, -1.525042e-01 + 0j, -1.525042e-01 + 0j, 0j, 0j, 0j]
POLES = [
    complex(-1.207063e-02, 1.224561e-02),
    complex(-1.207063e-02, -1.224561e-02),
    complex(-1.522510e-01, 9.643684e-03),
    complex(-1.522510e-01, -9.643684e-03),
    complex(-4.832398e01, 5.817080e01),
    complex(-4.832398e01, -5.817080e01),
]


def direct(f):
    """CONSTANT * prod(s - z) / prod(s - p) at s = 2*pi*i*f."""
    s = complex(0, 2 * math.pi * f)
    h = CONSTANT
    for z in ZEROS:
        h *= s - z
    for p in POLES:
        h /= s - p
    return h


def test_annotated_example():
    (chan,) = sr.read_response(PFO)
    assert (chan.network, chan.station, chan.location, chan.channel) == ("II", "PFO", "00", "BHZ")
    assert chan.start_time == datetime(2010, 7, 30, 18, 50, tzinfo=UTC)
    assert chan.end_time is None  # 2599 means open ended
    (st,) = chan.stages
    assert st.input_units.startswith("M ") and st.output_units.startswith("COUNTS")
    assert st.filter.kind == "laplace_rad"
    assert st.filter.zeros == ZEROS  # three listed, three padded with (0, 0)
    assert st.filter.poles == POLES
    assert st.decimation.input_sample_rate == 20.0


@pytest.mark.parametrize("f", [0.001, 0.02, 1.0, 7.5, 10.0])
def test_displacement_response_matches_the_transfer_function(f):
    (chan,) = sr.read_response(PFO)
    res = chan.evaluate([f], unit="dis")
    expected = direct(f)
    assert abs(res.values[0] - expected) <= 1e-10 * abs(expected)


def test_velocity_is_displacement_divided_by_i_omega():
    (chan,) = sr.read_response(PFO)
    f = 0.5
    vel = chan.evaluate([f], unit="vel").values[0]
    expected = direct(f) / complex(0, 2 * math.pi * f)
    assert abs(vel - expected) <= 1e-10 * abs(expected)


def test_sensitivity_is_referenced_to_one_hz():
    (chan,) = sr.read_response(PFO)
    info = chan.evaluate([1.0], unit="dis").info
    assert info.sensitivity_frequency == 1.0
    assert info.calculated_sensitivity == pytest.approx(abs(direct(1.0)), rel=1e-10)


def test_unannotated_defaults_and_mixed_case():
    (chan,) = parse_sacpz("zeros 1\npoles 2\n-1 1\n-1 -1\n")
    assert (chan.network, chan.station, chan.location, chan.channel) == ("", "SACPZ", "", "")
    assert chan.start_time is None and chan.stages[0].decimation is None
    assert chan.stages[0].filter.zeros == [0j]
    h = chan.evaluate([1.0], unit="dis").values[0]  # CONSTANT defaults to 1
    s = 2j * math.pi
    assert abs(h - s / ((s - (-1 + 1j)) * (s - (-1 - 1j)))) < 1e-12


def test_codes_from_the_rdseed_file_name():
    (chan,) = parse_sacpz("POLES 1\n-1 0\nCONSTANT 2\n", origin="SAC_PZs_IU_ANMO_BHZ_00_2008.123")
    assert (chan.network, chan.station, chan.location, chan.channel) == ("IU", "ANMO", "00", "BHZ")


def test_several_epochs_in_one_file():
    text = PFO.read_text()
    second = text.replace("2010-07-30T18:50:00", "2005-01-01T00:00:00").replace("2599-12-31T23:59:59", "2010-07-30T18:50:00")
    chans = parse_sacpz(second + text)
    assert len(chans) == 2
    early = sr.select_channel(chans, "FDSN:II_PFO_00_B_H_Z", time=datetime(2007, 1, 1, tzinfo=UTC))
    late = sr.select_channel(chans, "FDSN:II_PFO_00_B_H_Z", time=datetime(2012, 1, 1, tzinfo=UTC))
    assert early.start_time.year == 2005 and late.start_time.year == 2010


def test_several_unannotated_blocks_split_on_repeated_keywords():
    assert len(parse_sacpz("POLES 1\n-1 0\nCONSTANT 1\nPOLES 1\n-2 0\nCONSTANT 3\n")) == 2


def test_format_detection():
    text = PFO.read_text()
    assert looks_like_sacpz(text) and looks_like_sacpz(text.encode())
    assert not looks_like_sacpz((DATA / "real" / "RESP.IU.ANMO.00.BHZ").read_text())
    assert not looks_like_sacpz("<FDSNStationXML/>")
    assert len(sr.parse_response(text)) == 1  # auto
    assert len(sr.parse_response(text, fmt="pz")) == 1


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "no ZEROS"),
        ("* just a comment\n", "no ZEROS"),
        ("ZEROS x\n", "ZEROS needs"),
        ("ZEROS 31\n", "at most 30"),
        ("POLES 1\n-1 0\n-2 0\n", "more values"),
        ("POLES 2\n-1 0\n", "POLES 2 announced but 1 listed"),
        ("POLES 1\n-1 abc\n", "real and imaginary"),
        ("CONSTANT\n", "CONSTANT needs"),
        ("-1 0\n", "unrecognized"),
        ("* START : yesterday\nPOLES 1\n-1 0\n", "cannot parse time"),
    ],
)
def test_errors(text, message):
    with pytest.raises(ResponseParseError, match=message):
        parse_sacpz(text)


def test_gui_api_loads_and_evaluates():
    web_api.clear()
    assert json.loads(web_api.load("pfo.pz", PFO.read_text())) == {"ok": True, "added": 1, "load": 0}
    listed = json.loads(web_api.channels())
    assert listed[0]["source_id"] == "FDSN:II_PFO_00_B_H_Z"
    assert listed[0]["stages"][0]["sample_rate"] == 20.0  # gives the GUI its Nyquist (10 Hz)
    req = {"channel": 0, "frequencies": {"min": 0.01, "max": 10, "count": 3, "spacing": "log"}, "unit": "vel"}
    assert json.loads(web_api.evaluate_response(json.dumps(req)))["ok"] is True
    web_api.clear()


def test_instrument_from_insttype_annotation():
    (chan,) = sr.read_response(PFO)
    assert chan.instrument == "Streckeisen STS-1 Seismometer with Metrozet E300"
    assert parse_sacpz("POLES 1\n-1 0\nCONSTANT 2\n")[0].instrument == ""

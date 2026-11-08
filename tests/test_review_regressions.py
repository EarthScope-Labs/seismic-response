"""Regression tests for edge cases found in the package review."""

import json

import pytest
from conftest import DATA

import seismic_response as sr
from seismic_response import cli, web_api
from seismic_response.model import FIR, LAPLACE_RADIANS, Channel, Decimation, Gain, PolesZeros, ResponseList, Stage

_PZ = lambda: PolesZeros(LAPLACE_RADIANS, [], [], 1.0, 1.0)  # noqa: E731

_XML = """<?xml version="1.0"?><{p}FDSNStationXML {ns}><Network code="XX"><Station code="T">
<Channel code="BHZ" locationCode=""><Response><Stage number="1"><PolesZeros>
<InputUnits><Name>M/S</Name></InputUnits><OutputUnits><Name>V</Name></OutputUnits>
<PzTransferFunctionType>LAPLACE (RADIANS/SECOND)</PzTransferFunctionType>
<NormalizationFactor>1</NormalizationFactor><NormalizationFrequency>1</NormalizationFrequency>
<Pole number="{n}"><Real>-1</Real><Imaginary>0</Imaginary></Pole></PolesZeros>
<StageGain><Value>1</Value><Frequency>1</Frequency></StageGain></Stage></Response></Channel></Station></Network></{p}FDSNStationXML>"""


def _xml(prefix="", number="0"):
    ns = f'xmlns{":" + prefix.rstrip(":") if prefix else ""}="http://www.fdsn.org/xml/station/1"'
    return _XML.format(p=prefix, ns=ns, n=number)


def test_fir_summing_to_zero_is_a_package_error():
    ch = Channel("XX", "T", "", "BHZ", stages=[
        Stage(1, "M/S", "V", _PZ(), gain=Gain(100, 1.0)),
        Stage(2, "V", "COUNTS", FIR("A", [0.5, -0.5]), Decimation(100, 1), Gain(1, 1.0)),
    ])
    with pytest.raises(sr.ResponseValueError, match="sum to zero"):
        sr.evaluate(ch, [1.0])


def test_sensitivity_only_channel_is_rejected():
    ch = Channel("XX", "T", "", "BHZ", stages=[Stage(0, "M/S", "COUNTS", gain=Gain(1e9, 1.0))])
    with pytest.raises(sr.ResponseValueError, match="only an overall sensitivity"):
        sr.evaluate(ch, [1.0])


def test_per_stage_product_matches_total_with_total_sensitivity():
    ch = Channel("XX", "T", "", "BHZ", stages=[
        Stage(1, "M/S", "V", _PZ(), gain=Gain(100, 1.0)),
        Stage(2, "V", "COUNTS", gain=Gain(10, 1.0)),
        Stage(0, "M/S", "COUNTS", gain=Gain(2000, 1.0)),
    ])
    r = sr.evaluate(ch, [1.0], use_total_sensitivity=True, per_stage=True)
    prod = 1
    for v in r.stage_values.values():
        prod *= v[0]
    assert prod == pytest.approx(r.values[0])


def test_response_list_may_be_followed_by_gain_only_stage():
    ch = Channel("XX", "T", "", "BHZ", stages=[
        Stage(1, "M/S", "V", ResponseList([1.0, 2.0], [1.0, 1.0], [0, 0]), gain=Gain(1, 1.0)),
        Stage(2, "V", "COUNTS", gain=Gain(10, 1.0)),
    ])
    assert sr.evaluate(ch, [1.0]).values == [10 + 0j, 10 + 0j]


def test_gain_only_stages_keep_sign_and_sample_interval():
    ch = Channel("XX", "T", "", "BHZ", stages=[
        Stage(1, None, None, gain=Gain(-5, 1.0)),
        Stage(2, "M/S", "V", PolesZeros(LAPLACE_RADIANS, [], [-1 + 0j], 1.0, 1.0), gain=Gain(2, 1.0)),
        Stage(3, "V", "COUNTS", FIR("A", [1.0]), Decimation(100, 2), Gain(1, 1.0)),
        Stage(4, None, None, gain=Gain(3, 1.0)),
    ])
    r = sr.evaluate(ch, [1.0], unit="vel", per_stage=True)
    assert r.stage_values[1] == [-5 + 0j]
    assert r.stage_values[4] == [3 + 0j]
    assert r.info.calculated_sensitivity == -30
    assert r.info.sample_interval == pytest.approx(0.02)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_generate_frequencies_rejects_non_finite(bad):
    with pytest.raises(sr.ResponseValueError):
        sr.generate_frequencies(bad, 10, 3)


def test_stationxml_bad_number_attribute_is_parse_error():
    with pytest.raises(sr.ResponseParseError):
        sr.parse_response(_xml(number="x"))


def test_stationxml_prefixed_root_is_detected():
    assert sr.looks_like_stationxml(_xml(prefix="s:"))
    assert len(sr.parse_response(_xml(prefix="s:"))) == 1


def test_stationxml_detected_after_long_header():
    text = "<?xml version='1.0'?><!--" + "x" * 2000 + "-->" + _xml().split("?>", 1)[1]
    assert sr.looks_like_stationxml(text)


def test_web_api_select_bad_time_returns_error():
    assert json.loads(web_api.select("*", "garbage"))["ok"] is False


def test_cli_invalid_date_has_no_traceback(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.evalresp_main(["ANMO", "BHZ", "0", "1", "0.1", "1", "3", "-f", str(DATA / "real" / "RESP.IU.ANMO.00.BHZ")])
    assert rc == cli.EXIT_INPUT
    assert "Invalid year/day" in capsys.readouterr().err


def test_cli_continues_after_failing_channel(tmp_path, monkeypatch):
    good = (DATA / "real" / "RESP.IU.ANMO.00.BHZ").read_text()
    # a second file whose only stage gain is zeroed fails validation
    bad = "".join(
        line.replace(line.split(":", 1)[1], "      0.0\n") if line.startswith("B058F04") else line
        for line in good.splitlines(keepends=True)
    ).replace("IU", "IV")
    (tmp_path / "all.resp").write_text(bad + good)
    monkeypatch.chdir(tmp_path)
    rc = cli.evalresp_main(["*", "BHZ", "2000", "100", "0.1", "1", "3", "-f", "all.resp", "-r", "fap", "-n", "*"])
    assert rc == cli.EXIT_VALIDATION
    assert (tmp_path / "FAP.IU.ANMO.00.BHZ").exists()

"""RESP and StationXML parsing."""

from datetime import datetime, timezone

import pytest
from conftest import load_fixture

import seismic_response as sr
from seismic_response import FIR, PolesZeros, ResponseList
from seismic_response.resp import parse_resp, parse_resp_time
from seismic_response.timeutil import parse_iso_time

UTC = timezone.utc


# ------------------------------------------------------------------ RESP


def test_resp_header_and_stages(anmo):
    first, second = anmo
    assert (first.network, first.station, first.location, first.channel) == ("IU", "ANMO", "00", "BHZ")
    assert first.source_id == "FDSN:IU_ANMO_00_B_H_Z"
    assert first.start_time == datetime(1998, 10, 26, 20, 0, tzinfo=UTC)  # 1998,299,20
    assert first.end_time == datetime(2000, 10, 19, 16, 0, tzinfo=UTC)
    assert second.end_time is None  # "No Ending Time"
    assert [s.sequence for s in first.stages] == [1, 2, 3, 4, 5, 6, 0]

    s1 = first.stage(1)
    assert isinstance(s1.filter, PolesZeros) and s1.filter.kind == "laplace_rad"
    assert s1.filter.a0 == 86083.0 and s1.filter.a0_freq == 0.02
    assert s1.filter.zeros == [0j, 0j] and len(s1.filter.poles) == 5
    assert s1.input_units.startswith("M/S") and s1.output_units.startswith("V")
    assert s1.gain.value == 2061.68

    s3 = first.stage(3)
    assert isinstance(s3.filter, FIR) and len(s3.filter.coefficients) == 64
    assert s3.decimation.factor == 16 and s3.decimation.input_sample_rate == 5120.0
    assert first.sensitivity.value == 864730000.0 and first.sensitivity.frequency == 0.02


def test_resp_time_formats():
    assert parse_resp_time("1998,299,20") == datetime(1998, 10, 26, 20, tzinfo=UTC)
    assert parse_resp_time("1999,013") == datetime(1999, 1, 13, tzinfo=UTC)
    assert parse_resp_time("2001,001,00:00:00.5") == datetime(2001, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)
    assert parse_resp_time("No Ending Time") is None
    assert parse_resp_time("") is None
    with pytest.raises(ValueError):
        parse_resp_time("garbage,x")


RESP_MIN = """#
B050F03     Station:     {sta}
B050F16     Network:     {net}
B052F03     Location:    {loc}
B052F04     Channel:     BHZ
B052F22     Start date:  2000,001,00:00:00
B052F23     End date:    No Ending Time
B053F03     Transfer function type:                A [Laplace Transform (Rad/sec)]
B053F04     Stage sequence number:                 1
B053F05     Response in units lookup:              M/S - Velocity in Meters Per Second
B053F06     Response out units lookup:             V - Volts
B053F07     A0 normalization factor:               1.0
B053F08     Normalization frequency:               1.0
B053F09     Number of zeroes:                      0
B053F14     Number of poles:                       1
B053F15-18    0  -6.283185E+00  +0.000000E+00  +0.000000E+00  +0.000000E+00
B058F03     Stage sequence number:                 1
B058F04     Gain:                                  1.0
B058F05     Frequency of gain:                     1.0 HZ
B058F06     Number of calibrations:                0
"""


def test_resp_network_and_location_placeholders():
    # '??' for the network and location means "none"
    c = parse_resp(RESP_MIN.format(sta="ST", net="??", loc="??"))[0]
    assert c.network == "" and c.location == ""
    assert c.source_id == "FDSN:_ST__B_H_Z"
    # An entirely empty location field is also the empty location
    c = parse_resp(RESP_MIN.format(sta="ST", net="XX", loc=""))[0]
    assert c.location == ""


def test_resp_tolerates_crlf_and_tabs():
    text = RESP_MIN.format(sta="ST", net="XX", loc="00").replace("\n", "\r\n").replace("     ", "\t")
    c = parse_resp(text)[0]
    assert c.station == "ST" and c.stage(1).filter.poles == [complex(-6.283185, 0)]


def test_resp_accepts_any_field_order_for_coefficients():
    """SEED order (counts after the numerators) and evalresp's xml2resp order both parse."""
    seed_order = RESP_MIN.format(sta="A", net="X", loc="00") + (
        "B054F03     Transfer function type:                D\n"
        "B054F04     Stage sequence number:                 2\n"
        "B054F05     Response in units lookup:              V - Volts\n"
        "B054F06     Response out units lookup:             COUNTS - Digital Counts\n"
        "B054F07     Number of numerators:                  2\n"
        "B054F08-09    0  +5.000000E-01  +0.000000E+00\n"
        "B054F08-09    1  +5.000000E-01  +0.000000E+00\n"
        "B054F10     Number of denominators:                0\n"
    )
    f = parse_resp(seed_order)[0].stage(2).filter
    assert isinstance(f, FIR) and f.coefficients == [0.5, 0.5]


def test_resp_fir_continuation_blockettes_merge():
    text = RESP_MIN.format(sta="A", net="X", loc="00")
    for coeffs in ([0.1, 0.2], [0.3, 0.4]):
        text += (
            "B061F03     Stage sequence number:                 2\n"
            "B061F04     Response Name:                         X\n"
            "B061F05     Symmetry Code:                         A\n"
            "B061F06     Response in units lookup:              V - Volts\n"
            "B061F07     Response out units lookup:             COUNTS - Digital Counts\n"
            f"B061F08     Number of Coefficients:                {len(coeffs)}\n"
        ) + "".join(f"B061F09     {i:3d}  {c:+.6E}\n" for i, c in enumerate(coeffs))
    assert parse_resp(text)[0].stage(2).filter.coefficients == [0.1, 0.2, 0.3, 0.4]


def test_resp_response_list_rows_with_and_without_index():
    stage = (
        "B055F03     Stage sequence number:                 1\n"
        "B055F04     Response in units lookup:              M/S - Velocity\n"
        "B055F05     Response out units lookup:             V - Volts\n"
        "B055F06     Number of responses listed:            2\n"
        "B055F07-11     1  1.0E-01  2.0E+00  0.0E+00  1.0E+01  0.0E+00\n"
        "B055F07-11     2  1.0E+00  3.0E+00  0.0E+00  2.0E+01  0.0E+00\n"
    )
    header = RESP_MIN.split("B053F03")[0].format(sta="A", net="X", loc="00")
    f = parse_resp(header + stage)[0].stage(1).filter
    assert isinstance(f, ResponseList)
    assert f.frequencies == [0.1, 1.0] and f.amplitudes == [2.0, 3.0] and f.phases == [10.0, 20.0]
    no_index = stage.replace("    1  1.0E-01", " 1.0E-01").replace("    2  1.0E+00", " 1.0E+00")
    assert parse_resp(header + no_index)[0].stage(1).filter.frequencies == [0.1, 1.0]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda t: t.replace("B053F14     Number of poles:                       1\n", "B053F14     Number of poles:                       3\n"), "declared"),
        (lambda t: t.replace("A [Laplace", "Q [Laplace"), "unsupported"),
        (lambda t: t + "B060F01     Number of stages:    1\n", "blockette 60"),
        (lambda t: t.replace("B053F07     A0 normalization factor:               1.0\n", ""), "field 07 missing"),
    ],
)
def test_resp_errors(mutate, message):
    with pytest.raises(sr.ResponseParseError, match=message):
        parse_resp(mutate(RESP_MIN.format(sta="A", net="X", loc="00")))


def test_resp_without_channels():
    with pytest.raises(sr.ResponseParseError, match="No channel"):
        parse_resp("# just a comment\n")


# ------------------------------------------------------------------ StationXML


def test_stationxml_matches_xml2resp_output():
    """The XML reading equals evalresp's own XML->RESP conversion (which prints 6 digits)."""
    xml = load_fixture("real/station-3.xml")
    resp = load_fixture("real/station-3.xml2resp.resp")
    assert len(xml) == len(resp) == 1
    x, r = xml[0], resp[0]
    assert (x.nslc, x.start_time, x.end_time) == (r.nslc, r.start_time, r.end_time)
    assert [s.sequence for s in x.stages] == [s.sequence for s in r.stages]
    for sx, sr_ in zip(x.stages, r.stages):
        assert type(sx.filter) is type(sr_.filter)
        if isinstance(sx.filter, FIR):
            assert sx.filter.coefficients == pytest.approx(sr_.filter.coefficients, rel=2e-5, abs=1e-12)
            assert sx.filter.symmetry == sr_.filter.symmetry
        if isinstance(sx.filter, PolesZeros):
            assert sx.filter.poles == pytest.approx(sr_.filter.poles, rel=2e-5)
            assert sx.filter.a0 == pytest.approx(sr_.filter.a0, rel=2e-5)
        if sx.gain:
            assert sx.gain.value == pytest.approx(sr_.gain.value, rel=2e-5)
        if sx.decimation:
            assert sx.decimation.factor == sr_.decimation.factor


XML_MIN = """<?xml version="1.0" encoding="ISO-8859-1"?>
<FDSNStationXML xmlns="http://www.fdsn.org/xml/station/1" schemaVersion="1.0">
 <Network code="XX"><Station code="STA">
  <Channel code="BHZ" locationCode="" startDate="2010-01-01T00:00:00" {end}>
   <Response>
    <InstrumentSensitivity><Value>2000.0</Value><Frequency>1.0</Frequency>
     <InputUnits><Name>M/S</Name></InputUnits><OutputUnits><Name>V</Name><Description>Volts caf\xe9</Description></OutputUnits>
    </InstrumentSensitivity>
    <Stage number="1"><PolesZeros>
      <InputUnits><Name>M/S</Name></InputUnits><OutputUnits><Name>V</Name></OutputUnits>
      <PzTransferFunctionType>LAPLACE (HERTZ)</PzTransferFunctionType>
      <NormalizationFactor>1.5</NormalizationFactor><NormalizationFrequency>1.0</NormalizationFrequency>
      <Pole number="1"><Real>-2.0</Real><Imaginary>-3.0</Imaginary></Pole>
      <Pole number="0"><Real>-2.0</Real><Imaginary>3.0</Imaginary></Pole>
     </PolesZeros><StageGain><Value>2000.0</Value><Frequency>1.0</Frequency></StageGain></Stage>
   </Response>
  </Channel>
 </Station></Network>
</FDSNStationXML>
"""


def test_stationxml_minimal_document_and_pole_ordering():
    (c,) = sr.parse_response(XML_MIN.format(end='endDate="2020-01-01T00:00:00"'))
    assert c.source_id == "FDSN:XX_STA__B_H_Z"
    assert c.end_time == datetime(2020, 1, 1, tzinfo=UTC)
    pz = c.stage(1).filter
    assert pz.kind == "laplace_hz"
    assert pz.poles == [complex(-2, 3), complex(-2, -3)]  # sorted by the "number" attribute
    # Units: name only, or "name - description"
    assert c.stage(1).input_units == "M/S"
    assert c.stage(0).output_units.startswith("V - Volts caf")  # latin-1 text decoded
    assert c.sensitivity.value == 2000.0


def test_stationxml_open_ended_channel_and_bytes_input():
    (c,) = sr.parse_response(XML_MIN.format(end="").encode("latin-1"))
    assert c.end_time is None


def test_stationxml_errors():
    with pytest.raises(sr.ResponseParseError, match="Invalid XML"):
        sr.parse_stationxml("<FDSNStationXML><Network>")
    with pytest.raises(sr.ResponseParseError, match="Not a StationXML"):
        sr.parse_stationxml("<root/>")
    with pytest.raises(sr.ResponseParseError, match="No Channel"):
        sr.parse_stationxml('<FDSNStationXML xmlns="http://www.fdsn.org/xml/station/1"/>')
    analog = XML_MIN.format(end="").replace("LAPLACE (HERTZ)", "ANALOG NONSENSE")
    with pytest.raises(sr.ResponseParseError, match="PzTransferFunctionType"):
        sr.parse_response(analog)


def test_parse_response_autodetects_format():
    assert sr.parse_response(XML_MIN.format(end=""))[0].station == "STA"
    assert sr.parse_response(RESP_MIN.format(sta="ZZ", net="XX", loc="00"))[0].station == "ZZ"
    with pytest.raises(ValueError):
        sr.parse_response("x", fmt="nonsense")


# ------------------------------------------------------------------ times


def test_parse_iso_time_variants():
    assert parse_iso_time("2014-12-17") == datetime(2014, 12, 17, tzinfo=UTC)
    assert parse_iso_time("2014-12-17T18:40:00Z") == datetime(2014, 12, 17, 18, 40, tzinfo=UTC)
    assert parse_iso_time("2014-12-17 18:40:00.25").microsecond == 250000
    assert parse_iso_time("2014-12-17T18:40:00+01:00") == datetime(2014, 12, 17, 17, 40, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_iso_time("17/12/2014")

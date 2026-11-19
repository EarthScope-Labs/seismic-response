"""Channel selection: identifiers, globs and evalresp's epoch rules."""

from datetime import datetime, timezone

import pytest

import seismic_response as sr
from seismic_response import Channel
from seismic_response.resp import parse_resp_time
from seismic_response.timeutil import parse_iso_time

UTC = timezone.utc


def d(y, m=1, day=1):
    return datetime(y, m, day, tzinfo=UTC)


def ch(start, end, net="IU", sta="ANMO", loc="00", cha="BHZ"):
    return Channel(net, sta, loc, cha, start_time=start, end_time=end)


CHANNELS = [
    ch(d(2000), d(2005)),
    ch(d(2005), None),
    ch(d(2000), None, loc="10"),
    ch(d(2000), None, sta="COLA", cha="LHZ"),
    ch(d(2000), None, net="", sta="NONET", loc="", cha="BHZ"),
]


def names(found):
    return sorted((c.station, c.location, c.start_time.year) for c in found)


def test_no_time_prefers_latest_epoch_of_each_channel():
    found = sr.select_channels(CHANNELS)
    assert names(found) == [("ANMO", "00", 2005), ("ANMO", "10", 2000), ("COLA", "00", 2000), ("NONET", "", 2000)]
    # the open-ended 2005 epoch beats the closed 2000-2005 one
    anmo00 = [c for c in found if c.station == "ANMO" and c.location == "00"]
    assert len(anmo00) == 1 and anmo00[0].start_time == d(2005)


def test_time_selects_the_epoch_containing_it():
    found = sr.select_channels(CHANNELS, station="ANMO", location="00", time=d(2003))
    assert len(found) == 1 and found[0].end_time == d(2005)
    found = sr.select_channels(CHANNELS, station="ANMO", location="00", time=d(2010))
    assert found[0].start_time == d(2005)
    assert sr.select_channels(CHANNELS, station="ANMO", location="00", time=d(1990)) == []


def test_epoch_end_is_exclusive_and_start_inclusive():
    found = sr.select_channels(CHANNELS, station="ANMO", location="00", time=d(2005))
    assert [c.start_time for c in found] == [d(2005)]


def test_overlapping_epochs_choose_the_shortest():
    chans = [ch(d(2000), d(2010)), ch(d(2004), d(2006)), ch(d(2001), None)]
    found = sr.select_channels(chans, time=d(2005))
    assert len(found) == 1 and found[0].start_time == d(2004)


def test_glob_patterns_and_lists():
    assert len(sr.select_channels(CHANNELS, station="ANMO", time=d(2010))) == 2  # both locations
    assert {c.station for c in sr.select_channels(CHANNELS, station="ANMO,COLA", time=d(2010))} == {"ANMO", "COLA"}
    assert [c.channel for c in sr.select_channels(CHANNELS, channel="L??", time=d(2010))] == ["LHZ"]
    assert len(sr.select_channels(CHANNELS, location="1?", time=d(2010))) == 1


def test_empty_location_and_empty_network():
    # a location of only blanks selects the empty location; '??' selects any location
    assert [c.station for c in sr.select_channels(CHANNELS, location=[""], time=d(2010))] == ["NONET"]
    assert len(sr.select_channels(CHANNELS, location="??", time=d(2010))) == 4
    assert [c.station for c in sr.select_channels(CHANNELS, network=[""], time=d(2010))] == ["NONET"]
    assert [c.station for c in sr.select_channels(CHANNELS, network="??", station="NONET", time=d(2010))] == ["NONET"]


def test_source_id_glob():
    found = sr.select_channels(CHANNELS, "FDSN:IU_ANMO_*", time=d(2010))
    assert {c.location for c in found} == {"00", "10"}
    assert sr.select_channels(CHANNELS, "FDSN:IU_ANMO_00_B_H_Z", time=d(2003))[0].end_time == d(2005)


def test_source_id_prefix_is_optional():
    full = sr.select_channels(CHANNELS, "FDSN:IU_ANMO_*", time=d(2010))
    assert sr.select_channels(CHANNELS, "IU_ANMO_*", time=d(2010)) == full
    assert sr.select_channels(CHANNELS, "fdsn:IU_ANMO_*", time=d(2010)) == full
    assert len(sr.select_channels(CHANNELS, "F*", time=d(2010))) == len(sr.select_channels(CHANNELS, time=d(2010)))


def test_select_channel_requires_exactly_one():
    assert sr.select_channel(CHANNELS, "FDSN:IU_COLA_*").channel == "LHZ"
    with pytest.raises(sr.ChannelNotFoundError):
        sr.select_channel(CHANNELS, "FDSN:XX_*")
    with pytest.raises(ValueError, match="ambiguous"):
        sr.select_channel(CHANNELS, "FDSN:IU_ANMO_*", time=d(2010))


def test_source_id_round_trip():
    assert sr.nslc_to_source_id("XX", "TEST", "", "LHZ") == "FDSN:XX_TEST__L_H_Z"
    assert sr.source_id_to_nslc("FDSN:XX_TEST__L_H_Z") == ("XX", "TEST", "", "LHZ")
    with pytest.raises(ValueError):
        sr.source_id_to_nslc("FDSN:XX_TEST")


@pytest.mark.parametrize(
    "call, error",
    [
        (lambda: sr.select_channel(CHANNELS, "FDSN:IU_ANMO_*", time=d(2010)), sr.AmbiguousChannelError),
        (lambda: sr.source_id_to_nslc("FDSN:XX_TEST"), sr.ResponseValueError),
        (lambda: sr.parse_response("", fmt="nope"), sr.ResponseValueError),
        (lambda: sr.evaluate(CHANNELS[0], [1.0], unit="nope"), sr.ResponseValueError),
        (lambda: parse_iso_time("2020-13-01"), sr.ResponseParseError),
        (lambda: parse_iso_time("2020-01-01T00:00:00+24:00"), sr.ResponseParseError),
        (lambda: parse_resp_time("2020,x"), sr.ResponseParseError),
    ],
)
def test_argument_errors_are_package_errors(call, error):
    with pytest.raises(error) as info:
        call()
    assert isinstance(info.value, sr.SeismicResponseError)
    assert isinstance(info.value, ValueError)


def test_selected_epochs_keep_input_order_across_interleaved_channels():
    a_old, b_old = ch(d(2000), d(2005), sta="A"), ch(d(2000), d(2005), sta="B")
    b_new, a_new = ch(d(2005), None, sta="B"), ch(d(2005), None, sta="A")
    assert sr.select_channels([a_old, b_old, b_new, a_new]) == [b_new, a_new]
    assert sr.select_channels([a_old, b_old, b_new, a_new], time=d(2001)) == [a_old, b_old]

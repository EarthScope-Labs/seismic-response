"""Channel selection: identifiers, globs and evalresp's epoch rules."""

from datetime import datetime, timezone

import pytest

import seismic_response as sr
from seismic_response import Channel

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

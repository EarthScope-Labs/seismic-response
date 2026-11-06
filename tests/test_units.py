"""Unit-string classification (evalresp's parse_units)."""

import pytest

from seismic_response import units as U


@pytest.mark.parametrize(
    "text, klass, scale",
    [
        ("M/S - Velocity in Meters Per Second", U.VEL, 1.0),
        ("m/s", U.VEL, 1.0),
        ("M/S**2 - Acceleration", U.ACC, 1.0),
        ("M/SEC**2", U.ACC, 1.0),
        ("M - Displacement in Meters", U.DIS, 1.0),
        ("NM/S - Nanometers per second", U.VEL, 1e9),
        ("MM/S**2", U.ACC, 1e3),
        ("CM - centimetres", U.DIS, 1e2),
        ("COUNTS - Digital Counts", U.COUNTS, 1.0),
        ("DIGITAL UNIT in Counts", U.COUNTS, 1.0),
        ("V - Volts", U.VOLTS, 1.0),
        ("PA - Pascals", U.PRESSURE, 1.0),
        ("T - Tesla", U.TESLA, 1.0),
        ("NT - nanotesla", U.TESLA, 1e9),
        ("C - Degrees Centigrade", U.CENTIGRADE, 1.0),
        # spellings evalresp misclassifies by prefix matching
        ("M/S/S - Acceleration", U.ACC, 1.0),
        ("m/s/s", U.ACC, 1.0),
        ("M/S^2", U.ACC, 1.0),
        ("NM/S/S", U.ACC, 1e9),
        ("M/SEC/SEC", U.ACC, 1.0),
        ("M/S-velocity", U.VEL, 1.0),
        ("MBAR - millibar", U.UNDEF, 1.0),
        ("MICROVOLTS", U.UNDEF, 1.0),
        ("MPH", U.UNDEF, 1.0),
        ("M/S/MIN", U.UNDEF, 1.0),
        ("FURLONGS", U.UNDEF, 1.0),
        ("", U.UNDEF, 1.0),
        (None, U.UNDEF, 1.0),
    ],
)
def test_classify_units(text, klass, scale):
    assert U.classify_units(text) == (klass, scale)

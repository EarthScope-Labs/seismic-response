"""Classification of response unit strings, following evalresp's ``parse_units``.

evalresp inspects the (upper-cased) unit description of the first stage to decide
whether the response is relative to displacement, velocity or acceleration and
what scale factor converts a nm/mm/cm length unit to meters.  Those results
drive the optional conversion to a requested output unit (``dis``/``vel``/``acc``).

Motion units are matched exactly: the unit *symbol* (the text before the first
space, hyphen, comma or parenthesis) must be one of the known spellings; anything
else is unclassified.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

__all__ = [
    "DIS",
    "VEL",
    "ACC",
    "COUNTS",
    "VOLTS",
    "PRESSURE",
    "TESLA",
    "CENTIGRADE",
    "DEFAULT",
    "UNDEF",
    "classify_units",
    "motion_unit_name",
]

# Unit classes (names follow evalresp)
UNDEF = 0
DIS = 1
VEL = 2
ACC = 3
COUNTS = 4
VOLTS = 5
PRESSURE = 6
TESLA = 7
CENTIGRADE = 8
DEFAULT = 9

# A length unit with an optional SI prefix, optionally per second or per second squared.
_MOTION_RE = re.compile(
    r"^(?P<prefix>[NMC]?)M"
    r"(?P<rest>|/S|/SEC|/S\*\*2|/S\^2|/S2|/S/S|/SEC\*\*2|/SEC\^2|/SEC2|/SEC/SEC)$"
)
_SYMBOL_END_RE = re.compile(r"[\s\-,;(]")
_COUNTS_RE = re.compile(r"^COUNTS?[^A-Z]?|^DIGITAL[^A-Z]?")
_VOLTS_RE = re.compile(r"^V[^A-Z]?|^VOLTS[^A-Z]?")

_LENGTH_SCALE = {"N": 1.0e9, "M": 1.0e3, "C": 1.0e2}


def classify_units(text: Optional[str]) -> Tuple[int, float]:
    """Return ``(unit_class, scale)`` for a response units string.

    ``scale`` is the factor that converts a length unit with an SI prefix
    (nm, mm, cm) to meters.  It is only meaningful for the *first* stage's input
    units, which is how evalresp uses it.

    >>> classify_units("M/S - Velocity in Meters Per Second")
    (2, 1.0)
    >>> classify_units("NM/S**2 - Nanometers per second squared")
    (3, 1000000000.0)
    >>> classify_units("M/S/S - Acceleration")
    (3, 1.0)
    >>> classify_units("MBAR - millibar")
    (0, 1.0)
    """
    if not text:
        return UNDEF, 1.0

    line = text.upper()

    if line.startswith("PA"):
        return PRESSURE, 1.0
    if line[:2] == "KP":  # evalresp compares only two characters of "KPA"
        return PRESSURE, 1.0
    if line.startswith("T -"):
        return TESLA, 1.0
    if line.startswith("NT -"):
        return TESLA, 1.0e9
    if line.startswith("C -"):
        return CENTIGRADE, 1.0

    m = _MOTION_RE.match(_SYMBOL_END_RE.split(line.strip(), 1)[0])
    if m:
        rest = m.group("rest")
        unit_class = DIS if not rest else VEL if rest in ("/S", "/SEC") else ACC
        return unit_class, _LENGTH_SCALE.get(m.group("prefix"), 1.0)

    if _COUNTS_RE.match(line):
        return COUNTS, 1.0
    if _VOLTS_RE.match(line):
        return VOLTS, 1.0

    return UNDEF, 1.0


def motion_unit_name(unit_class: int) -> str:
    """Human readable name for a requested output unit class."""
    return {
        DIS: "Displacement (m)",
        VEL: "Velocity (m/s)",
        ACC: "Acceleration (m/s**2)",
        DEFAULT: "Documented response unit",
    }.get(unit_class, "Unrecognized response unit")

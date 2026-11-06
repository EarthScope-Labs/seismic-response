"""Parser for FDSN StationXML responses.

Only the parts needed to evaluate a response are read: the network/station/channel
codes and epoch, and the ``Response`` element with its stages.  The mapping follows
the conversion evalresp performs from StationXML to RESP.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Optional, Union

from .errors import ResponseParseError
from .model import (
    FIR,
    LAPLACE_HERTZ,
    LAPLACE_RADIANS,
    Z_TRANSFORM,
    Channel,
    Decimation,
    Gain,
    IIRCoefficients,
    PolesZeros,
    Polynomial,
    ResponseList,
    Stage,
)
from .timeutil import parse_iso_time

__all__ = ["parse_stationxml", "looks_like_stationxml"]


_ROOT_RE = re.compile(r"<(?:[\w.-]+:)?FDSNStationXML[\s>/]")


def looks_like_stationxml(text: Union[str, bytes]) -> bool:
    """True if the first 4 KB contain an ``FDSNStationXML`` root element (any namespace prefix)."""
    head = text[:4096]
    if isinstance(head, bytes):
        head = head.decode("utf-8", errors="replace")
    return _ROOT_RE.search(head) is not None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _children(elem: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in elem if _local(c.tag) == name]


def _text(elem: Optional[ET.Element], name: Optional[str] = None, default: Optional[str] = None) -> str:
    target = _child(elem, name) if (elem is not None and name is not None) else elem
    if target is None or target.text is None or not target.text.strip():
        if default is not None:
            return default
        raise ResponseParseError(f"StationXML element <{name}> is missing or empty")
    return target.text.strip()


def _float(elem: Optional[ET.Element], name: Optional[str] = None, default: Optional[float] = None) -> float:
    text = _text(elem, name, None if default is None else repr(default))
    try:
        return float(text)
    except ValueError:
        raise ResponseParseError(f"StationXML element <{name}>: cannot convert {text!r} to a number") from None


def _to_int(value: float, name: str) -> int:
    try:
        return int(value)
    except (ValueError, OverflowError):
        raise ResponseParseError(f"StationXML element <{name}>: {value!r} is not a valid integer") from None


def _int(text: Optional[str], what: str) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        raise ResponseParseError(f"StationXML {what}: cannot convert {text!r} to an integer") from None


def _number(text: str, name: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ResponseParseError(f"StationXML element <{name}>: cannot convert {text!r} to a number") from None


def _units(elem: ET.Element, name: str) -> Optional[str]:
    u = _child(elem, name)
    if u is None:
        return None
    unit_name = _text(u, "Name", "")
    desc = _text(u, "Description", "")
    return f"{unit_name} - {desc}" if desc else unit_name


def parse_stationxml(source: Union[str, bytes], origin: str = "") -> List[Channel]:
    """Parse StationXML text or bytes into a list of channel epochs."""
    if isinstance(source, str):
        # Decode ourselves: ignore any encoding declaration in the document
        data = io.BytesIO(source.encode("utf-8"))
        parser = ET.XMLParser(encoding="utf-8")
    else:
        data = io.BytesIO(source)
        parser = ET.XMLParser()

    channels: List[Channel] = []
    network = station = ""
    seen_root = False

    try:
        for event, elem in ET.iterparse(data, events=("start", "end"), parser=parser):
            tag = _local(elem.tag)
            if event == "start":
                if not seen_root:
                    seen_root = True
                    if tag != "FDSNStationXML":
                        raise ResponseParseError(f"Not a StationXML document (root element <{tag}>)")
                elif tag == "Network":
                    network = elem.get("code", "")
                elif tag == "Station":
                    station = elem.get("code", "")
            elif tag == "Channel":
                channels.append(_build_channel(elem, network, station, origin))
                elem.clear()
    except ET.ParseError as exc:
        raise ResponseParseError(f"Invalid XML: {exc}") from None

    if not channels:
        raise ResponseParseError("No Channel elements found in StationXML")
    return channels


def _date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_iso_time(value)
    except ValueError as exc:
        raise ResponseParseError(str(exc)) from None


def _build_channel(elem: ET.Element, network: str, station: str, origin: str) -> Channel:
    chan = Channel(
        network=network,
        station=station,
        location=(elem.get("locationCode") or "").strip(),
        channel=elem.get("code", ""),
        start_time=_date(elem.get("startDate")),
        end_time=_date(elem.get("endDate")),
        origin=origin,
    )
    sensor = _child(elem, "Sensor")
    if sensor is not None:
        chan.instrument = _text(sensor, "Description", "") or _text(sensor, "Type", "")
    response = _child(elem, "Response")
    if response is None:
        return chan  # channel without a response; evaluating it will report that

    try:
        for stage_elem in _children(response, "Stage"):
            chan.stages.append(_build_stage(stage_elem))

        sens = _child(response, "InstrumentSensitivity")
        if sens is not None:
            value = _float(sens, "Value", 0.0)
            if value != 0:  # evalresp ignores a zero sensitivity
                chan.stages.append(
                    Stage(
                        sequence=0,
                        input_units=_units(sens, "InputUnits"),
                        output_units=_units(sens, "OutputUnits"),
                        gain=Gain(value, _float(sens, "Frequency", 0.0)),
                    )
                )

        poly = _child(response, "InstrumentPolynomial")
        if poly is not None:
            stage = Stage(sequence=0)
            _fill_polynomial(stage, poly)
            chan.stages.append(stage)
    except ResponseParseError as exc:
        raise ResponseParseError(f"{chan.source_id}: {exc}") from None
    return chan


def _build_stage(elem: ET.Element) -> Stage:
    try:
        number = int(elem.get("number", ""))
    except ValueError:
        raise ResponseParseError("Stage element has no valid 'number' attribute") from None
    stage = Stage(sequence=number)

    pz = _child(elem, "PolesZeros")
    coeffs = _child(elem, "Coefficients")
    rlist = _child(elem, "ResponseList")
    fir = _child(elem, "FIR")
    poly = _child(elem, "Polynomial")

    present = [x for x in (pz, coeffs, rlist, fir, poly) if x is not None]
    if len(present) > 1:
        raise ResponseParseError(f"stage {number} has more than one filter type")

    if pz is not None:
        _fill_poles_zeros(stage, pz)
    elif coeffs is not None:
        _fill_coefficients(stage, coeffs)
    elif rlist is not None:
        _fill_response_list(stage, rlist)
    elif fir is not None:
        _fill_fir(stage, fir)
    elif poly is not None:
        _fill_polynomial(stage, poly)

    dec = _child(elem, "Decimation")
    if dec is not None:
        stage.decimation = Decimation(
            input_sample_rate=_float(dec, "InputSampleRate"),
            factor=_to_int(_float(dec, "Factor"), "Factor"),
            offset=_to_int(_float(dec, "Offset", 0.0), "Offset"),
            estimated_delay=_float(dec, "Delay", 0.0),
            correction_applied=_float(dec, "Correction", 0.0),
        )

    gain = _child(elem, "StageGain")
    if gain is not None:
        stage.gain = Gain(_float(gain, "Value"), _float(gain, "Frequency", 0.0))
    return stage


def _set_units(stage: Stage, elem: ET.Element) -> None:
    stage.input_units = _units(elem, "InputUnits")
    stage.output_units = _units(elem, "OutputUnits")


def _fill_poles_zeros(stage: Stage, elem: ET.Element) -> None:
    _set_units(stage, elem)
    ttype = _text(elem, "PzTransferFunctionType").upper()
    kinds = {
        "LAPLACE (RADIANS/SECOND)": LAPLACE_RADIANS,
        "LAPLACE (HERTZ)": LAPLACE_HERTZ,
        "DIGITAL (Z-TRANSFORM)": Z_TRANSFORM,
    }
    if ttype not in kinds:
        raise ResponseParseError(f"unsupported PzTransferFunctionType {ttype!r}")

    def collect(name: str) -> List[complex]:
        items = []
        for z in _children(elem, name):
            number = _int(z.get("number", str(len(items))), f"<{name}> number")
            items.append((number, complex(_float(z, "Real"), _float(z, "Imaginary"))))
        return [v for _, v in sorted(items, key=lambda x: x[0])]

    stage.filter = PolesZeros(
        kind=kinds[ttype],
        zeros=collect("Zero"),
        poles=collect("Pole"),
        a0=_float(elem, "NormalizationFactor"),
        a0_freq=_float(elem, "NormalizationFrequency"),
    )


def _indexed(elem: ET.Element, name: str) -> List[float]:
    items = []
    for c in _children(elem, name):
        idx = c.get("i")
        items.append((_int(idx, f"<{name}> index") if idx is not None else len(items), _number(_text(c), name)))
    return [v for _, v in sorted(items, key=lambda x: x[0])]


def _fill_coefficients(stage: Stage, elem: ET.Element) -> None:
    _set_units(stage, elem)
    ttype = _text(elem, "CfTransferFunctionType").upper()
    if ttype != "DIGITAL":
        raise ResponseParseError(
            f"unsupported coefficient transfer function type {ttype!r}; "
            "analog filters should be given as poles and zeros"
        )
    numerators = _indexed(elem, "Numerator")
    denominators = _indexed(elem, "Denominator")
    if denominators:
        stage.filter = IIRCoefficients(numerators, denominators)
    else:
        stage.filter = FIR("A", numerators)


def _fill_response_list(stage: Stage, elem: ET.Element) -> None:
    _set_units(stage, elem)
    freqs, amps, phases = [], [], []
    for e in _children(elem, "ResponseListElement"):
        freqs.append(_float(e, "Frequency"))
        amps.append(_float(e, "Amplitude"))
        phases.append(_float(e, "Phase"))
    stage.filter = ResponseList(freqs, amps, phases)


def _fill_fir(stage: Stage, elem: ET.Element) -> None:
    _set_units(stage, elem)
    symmetry = {"EVEN": "C", "ODD": "B"}.get(_text(elem, "Symmetry", "NONE").upper(), "A")
    stage.filter = FIR(symmetry, _indexed(elem, "NumeratorCoefficient"))


def _fill_polynomial(stage: Stage, elem: ET.Element) -> None:
    _set_units(stage, elem)
    items = []
    for c in _children(elem, "Coefficient"):
        items.append((_int(c.get("number", str(len(items))), "<Coefficient> number"), _number(_text(c), "Coefficient")))
    stage.filter = Polynomial(
        coefficients=[v for _, v in sorted(items, key=lambda x: x[0])],
        approximation_type="M",
        frequency_units="B",
        lower_freq_bound=_float(elem, "FrequencyLowerBound", 0.0),
        upper_freq_bound=_float(elem, "FrequencyUpperBound", 0.0),
        lower_approx_bound=_float(elem, "ApproximationLowerBound", 0.0),
        upper_approx_bound=_float(elem, "ApproximationUpperBound", 0.0),
        max_abs_error=_float(elem, "MaximumError", 0.0),
    )

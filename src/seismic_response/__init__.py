"""Evaluate seismological instrument responses.

Read a StationXML, SEED RESP or SAC poles-and-zeros response, pick a channel and time, and compute the
complex frequency response with results that match the C ``evalresp`` program::

    import seismic_response as sr

    channels = sr.read_response("RESP.IU.ANMO.00.BHZ")
    chan = sr.select_channel(channels, "FDSN:IU_ANMO_00_B_H_Z", time=datetime(2000, 1, 1, tzinfo=timezone.utc))
    freqs = sr.generate_frequencies(0.001, 50, 200)
    result = chan.evaluate(freqs, unit="vel")
    print(result.amplitudes[0], result.phases(unwrap=True)[0])

The package is pure Python (no dependencies) so it also runs under Pyodide.
"""

__version__ = "0.1.0"

from pathlib import Path
from typing import List, Union

from .errors import (
    ChannelNotFoundError,
    ResponseParseError,
    ResponseValueError,
    SeismicResponseError,
    UnsupportedFeatureError,
)
from .evaluate import (
    ResponseInfo,
    ResponseResult,
    StageInfo,
    evaluate,
    generate_frequencies,
    phase_degrees,
)
from .model import (
    FIR,
    Channel,
    Decimation,
    Gain,
    Generic,
    IIRCoefficients,
    PolesZeros,
    Polynomial,
    ResponseList,
    Stage,
    nslc_to_source_id,
    source_id_to_nslc,
)
from .resp import looks_like_resp, parse_resp
from .sacpz import looks_like_sacpz, parse_sacpz
from .selection import select_channel, select_channels
from .stationxml import looks_like_stationxml, parse_stationxml

__all__ = [
    "__version__",
    "read_response",
    "parse_response",
    "evaluate",
    "generate_frequencies",
    "select_channel",
    "select_channels",
    "phase_degrees",
    "parse_resp",
    "parse_stationxml",
    "parse_sacpz",
    "looks_like_sacpz",
    "looks_like_resp",
    "looks_like_stationxml",
    "Channel",
    "Stage",
    "PolesZeros",
    "FIR",
    "IIRCoefficients",
    "ResponseList",
    "Polynomial",
    "Generic",
    "Decimation",
    "Gain",
    "ResponseResult",
    "ResponseInfo",
    "StageInfo",
    "nslc_to_source_id",
    "source_id_to_nslc",
    "SeismicResponseError",
    "ResponseParseError",
    "ResponseValueError",
    "ChannelNotFoundError",
    "UnsupportedFeatureError",
]


def parse_response(content: Union[str, bytes], origin: str = "", fmt: str = "auto") -> List[Channel]:
    """Parse StationXML, SAC poles-and-zeros or RESP content.

    ``fmt`` is ``"auto"`` (detect), ``"stationxml"``, ``"sacpz"`` or ``"resp"``.  Like
    evalresp, anything that is neither StationXML nor SAC PZ is treated as RESP.
    """
    fmt = fmt.lower()
    if fmt == "auto":
        if looks_like_stationxml(content):
            fmt = "stationxml"
        elif looks_like_sacpz(content):
            fmt = "sacpz"
        else:
            fmt = "resp"
    if fmt in ("stationxml", "xml", "sxml"):
        return parse_stationxml(content, origin)
    if fmt in ("sacpz", "pz", "sac"):
        return parse_sacpz(content, origin)
    if fmt == "resp":
        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        return parse_resp(text, origin)
    raise ValueError(f"Unknown response format {fmt!r}")


def read_response(path: Union[str, Path], fmt: str = "auto") -> List[Channel]:
    """Read all channel epochs from a StationXML, SAC PZ or RESP file."""
    p = Path(path)
    return parse_response(p.read_bytes(), origin=p.name, fmt=fmt)

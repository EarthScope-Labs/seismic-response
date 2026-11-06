"""JSON API used by the interactive GUI.

Every function takes and returns plain strings/JSON so that the identical code can
be driven either from JavaScript through Pyodide (the browser runs this Python
via WebAssembly) or over HTTP by the local GUI server.  The module is
dependency free and keeps loaded responses in a process-wide session store.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

from . import __version__, parse_response
from .errors import ChannelNotFoundError, SeismicResponseError
from .evaluate import evaluate, generate_frequencies
from .model import Channel
from .output import render
from .selection import select_channels
from .timeutil import format_iso_time, parse_iso_time

__all__ = ["load", "clear", "channels", "select", "evaluate_response", "export_text", "version", "call"]

_store: List[Channel] = []
_loads: List[int] = []  # load number of each _store entry
_next_load = 0


def _num(x: float) -> Optional[float]:
    """JSON cannot carry inf/nan; send ``null`` instead."""
    return x if (isinstance(x, float) and math.isfinite(x)) or isinstance(x, int) else None


def _stage_json(st) -> Dict[str, Any]:
    d = {
        "sequence": st.sequence,
        "kind": st.filter_kind,
        "input_units": st.input_units,
        "output_units": st.output_units,
        "gain": _num(st.gain.value) if st.gain else None,
        "gain_frequency": _num(st.gain.frequency) if st.gain else None,
        "sample_rate": _num(st.decimation.input_sample_rate) if st.decimation else None,
        "decimation_factor": st.decimation.factor if st.decimation else None,
    }
    return d


def _channel_json(index: int, c: Channel) -> Dict[str, Any]:
    return {
        "id": index,
        "load": _loads[index],
        "source_id": c.source_id,
        "nslc": c.nslc,
        "network": c.network,
        "station": c.station,
        "location": c.location,
        "channel": c.channel,
        "start": format_iso_time(c.start_time),
        "end": format_iso_time(c.end_time),
        "origin": c.origin,
        "instrument": c.instrument,
        "stages": [_stage_json(s) for s in c.stages],
    }


def version() -> str:
    """Package version."""
    return json.dumps({"version": __version__})


def clear() -> str:
    """Forget all loaded responses."""
    global _next_load
    _store.clear()
    _loads.clear()
    _next_load = 0
    return json.dumps({"ok": True})


def load(name: str, content: str) -> str:
    """Parse StationXML/RESP text and add its channels to the session.

    Returns ``{"ok": true, "added": n, "load": k}`` or ``{"ok": false, "error": "..."}``,
    where ``k`` numbers this call (0, 1, ...) and tags its channels.
    """
    global _next_load
    try:
        found = parse_response(content, origin=name)
    except SeismicResponseError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except Exception as exc:  # malformed input must never break the UI
        return json.dumps({"ok": False, "error": f"Could not read {name!r}: {exc}"})
    load_id = _next_load
    _next_load += 1
    _store.extend(found)
    _loads.extend([load_id] * len(found))
    return json.dumps({"ok": True, "added": len(found), "load": load_id})


def channels() -> str:
    """All loaded channel epochs."""
    return json.dumps([_channel_json(i, c) for i, c in enumerate(_store)])


def select(source_id: str = "*", time: Optional[str] = None, load: Optional[int] = None) -> str:
    """Channel ids matching a Source ID glob (and time), following evalresp's epoch rules.

    A non-null ``load`` restricts the candidates to channels from that load.
    """
    try:
        t = parse_iso_time(time) if time else None
        wanted = int(load) if load is not None else None
    except (ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    pattern = source_id or "*"
    pool = [c for c, n in zip(_store, _loads) if wanted is None or n == wanted]
    found = select_channels(pool, pattern, time=t)
    ids = [i for i, c in enumerate(_store) if any(c is f for f in found)]
    return json.dumps(ids)


def _request_channel(req: Dict[str, Any]) -> Channel:
    try:
        return _store[int(req["channel"])]
    except (KeyError, ValueError, IndexError):
        raise ChannelNotFoundError("No such channel loaded") from None


def _compute(req: Dict[str, Any]):
    chan = _request_channel(req)
    fr = req.get("frequencies", {})
    freqs = generate_frequencies(
        float(fr.get("min", 0.0001)),
        float(fr.get("max", 50.0)),
        int(fr.get("count", 200)),
        linear=fr.get("spacing", "log") == "lin",
    )
    stages = req.get("stages")
    if stages is not None:
        stages = (stages[0], stages[1])
    return evaluate(
        chan,
        freqs,
        unit=req.get("unit", "def"),
        stages=stages,
        use_total_sensitivity=bool(req.get("total_sensitivity", False)),
        use_estimated_delay=bool(req.get("estimated_delay", False)),
        b62_x=float(req.get("b62_x", 0.0)),
        per_stage=bool(req.get("per_stage", False)),
        exclude_stages=[int(x) for x in req.get("exclude_stages") or []] or None,
    )


def evaluate_response(request: str) -> str:
    """Evaluate a response; ``request`` is a JSON object (see :func:`_compute`)."""
    try:
        req = json.loads(request)
        res = _compute(req)
    except SeismicResponseError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except (ValueError, TypeError, KeyError) as exc:
        return json.dumps({"ok": False, "error": f"Invalid request: {exc}"})

    unwrap = bool(req.get("unwrap", False))
    out: Dict[str, Any] = {
        "ok": True,
        "frequencies": res.frequencies,
        "amplitude": [_num(a) for a in res.amplitudes],
        "phase": [_num(p) for p in res.phases(unwrap=unwrap)],
        "real": [_num(v.real) for v in res.values],
        "imag": [_num(v.imag) for v in res.values],
        "unit": res.unit,
        "info": {
            "input_units": res.info.input_units,
            "output_units": res.info.output_units,
            "requested_unit": res.info.requested_unit,
            "reported_sensitivity": _num(res.info.reported_sensitivity),
            "calculated_sensitivity": _num(res.info.calculated_sensitivity),
            "sensitivity_frequency": _num(res.info.sensitivity_frequency),
            "stages": [
                {
                    "sequence": s.sequence,
                    "kind": s.kind,
                    "gain": _num(s.gain) if s.gain is not None else None,
                    "gain_frequency": _num(s.gain_frequency) if s.gain_frequency is not None else None,
                    "description": s.description,
                    "selected": s.selected,
                }
                for s in res.info.stages
            ],
        },
        "warnings": res.info.warnings,
    }
    if res.stage_values:
        out["stage_values"] = {
            str(seq): {
                "amplitude": [_num(abs(v)) for v in vals],
                "phase": [_num(p) for p in _phases(vals, unwrap)],
            }
            for seq, vals in res.stage_values.items()
        }
    return json.dumps(out)


def _phases(values, unwrap):
    from .evaluate import phase_degrees

    return phase_degrees(values, unwrap=unwrap)


def export_text(request: str, fmt: str = "fap") -> str:
    """Response as evalresp text output (``fap``, ``ap`` or ``cs``).

    For ``ap`` the amplitude and phase listings are joined, each preceded by its
    file name as a ``#`` comment line.
    """
    try:
        req = json.loads(request)
        res = _compute(req)
        parts = render(res, fmt, unwrap=bool(req.get("unwrap", False)))
    except (SeismicResponseError, ValueError, TypeError, KeyError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    if len(parts) == 1:
        return json.dumps({"ok": True, "filename": parts[0][0], "text": parts[0][1]})
    text = "".join(f"# {name}\n{body}" for name, body in parts)
    return json.dumps({"ok": True, "filename": f"AMPPHASE.{res.channel.nslc}", "text": text})


_FUNCTIONS = {
    "version": version,
    "load": load,
    "clear": clear,
    "channels": channels,
    "select": select,
    "evaluate": evaluate_response,
    "export": export_text,
}


def call(name: str, *args: Any) -> str:
    """Dispatch by name (used by both the HTTP server and the Pyodide bridge)."""
    try:
        func = _FUNCTIONS[name]
    except KeyError:
        return json.dumps({"ok": False, "error": f"Unknown function {name!r}"})
    return func(*args)

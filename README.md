# seismic-response

Evaluate seismological instrument responses from **StationXML**, **SEED RESP** or **SAC poles-and-zeros** files and get results that
match the C [`evalresp`](https://github.com/EarthScope/evalresp) program, as a

* **Python library**: pure Python, no dependencies, runs unchanged under [Pyodide](https://pyodide.org) (WebAssembly)
* **evalresp-compatible command line**: `pyevalresp`
* **interactive GUI**: a web app (plots amplitude and phase, per-stage curves, FDSN fetch, export) that runs
  locally, as a desktop window, or entirely in the browser with no server

Requires Python 3.11 or newer (Linux, macOS and Windows).

## Install

```sh
pip install seismic-response              # library + CLI + GUI
pip install "seismic-response[desktop]"   # also a native window for the GUI (pywebview)
```

## Library

```python
from datetime import datetime, timezone
import seismic_response as sr

channels = sr.read_response("RESP.IU.ANMO.00.BHZ")      # StationXML, RESP or SAC PZ, auto-detected
chan = sr.select_channel(channels, "FDSN:IU_ANMO_00_B_H_Z",
                         time=datetime(2000, 1, 1, tzinfo=timezone.utc))

freqs = sr.generate_frequencies(0.001, 50, 200)          # log spaced; linear=True for linear
result = chan.evaluate(freqs, unit="vel")                # "def" (default, as documented), "vel", "dis" or "acc"

result.frequencies          # Hz
result.values               # complex response, counts per unit of the requested ground motion
result.amplitudes
result.phases(unwrap=True)  # degrees
result.info                 # sensitivity, per-stage gains, warnings, ...
```

Useful options of `evaluate`: `stages=(first, last)`, `use_total_sensitivity=True`,
`use_estimated_delay=True` (asymmetric FIR), `b62_x=...` (polynomial responses) and `per_stage=True`
(each stage's contribution in `result.stage_values`; their product is the total response).

Channel selection follows evalresp: patterns are globs, and when several epochs of a channel overlap the
shortest one containing the requested time wins (the latest one when no time is given):

```python
sr.select_channels(channels, network="IU", station="ANMO", location="00,10", channel="BH?", time=t)
```

Errors derive from `SeismicResponseError` (`ResponseParseError`, `ResponseValueError`,
`ChannelNotFoundError`, `UnsupportedFeatureError`).

## Command line

`pyevalresp` takes the same arguments and options as the C `evalresp` and writes the same files
(`AMP.*`/`PHASE.*`, `FAP.*` or `SPECTRA.*`, or the `-stdio` form):

```sh
pyevalresp ANMO BHZ 2000 100 0.001 50 200 -f RESP.IU.ANMO.00.BHZ -u vel -r fap
pyevalresp KONO BHN,BHE 1992 1 0.001 10 100 -f resp_dir -t 12:31:04 -v
cat station.xml | pyevalresp '*' 'BH?' 2020 5 0.01 10 50 -stdio -x
```

Run `pyevalresp` without arguments for the full option list. Differences from the C program:
messages go to stderr, a request matching no channel prints a warning, and `-il`/`-ii` are rejected
(see *Limitations*). Also, `-u` defaults to `def` (the units documented in the response) rather than
evalresp's `vel`; pass `-u vel` to get evalresp's behavior.

`seismic-response` is the umbrella command: `seismic-response info FILE [-s]` lists channels and stages,
`seismic-response evalresp ...` is `pyevalresp`, and `seismic-response gui [FILE ...]` starts the GUI.

## GUI

```sh
seismic-response gui RESP.IU.ANMO.00.BHZ       # opens your browser on a local server
seismic-response gui --app                     # native window (needs the [desktop] extra)
python -m seismic_response.gui --build site/   # static copy: host it anywhere, no Python server needed
```

The browser version of each release is published to GitHub Pages at
<https://earthscope-labs.github.io/seismic-response/>. A static copy has to be served over HTTP
(for example `python -m http.server -d site/`); opening `index.html` from disk does not work.

Load files by dropping them on the page (or fetch from an FDSN station web service), choose an FDSN Source
ID and a point in time (the epoch containing it is chosen as in evalresp), then set the frequency range,
output unit and options, and tick the stages to include in the stage table. The plot shows amplitude and phase with a hover read-out; individual
stages can be overlaid; results export as evalresp text (FAP, AP, complex), SVG or PNG.

The page talks to the same Python through a small JSON API (`seismic_response.web_api`). With the local
server that is plain HTTP on the loopback interface (requests with a foreign `Host` or `Origin` are
refused). On a static host it loads Pyodide from a CDN and runs the package in the browser.

## How closely does it match evalresp?

The numerical code is a port of evalresp's own logic (stage consistency checks, gain/normalization
handling, FIR/IIR/pole-zero evaluation, unit conversion, phase unwrapping) rather than a re-derivation.
It is checked against a built evalresp 5.0.1 in two ways:

* **Golden tests** (`tests/test_golden.py`, run everywhere): 167 results captured from the C program for
  real and synthetic responses over a matrix of options, matched to within the 7 digits it prints.
* **Live comparison** (`tools/compare_evalresp.py BINARY FILES...`, needs a built C evalresp): 2,509
  complex-spectrum comparisons over every response in evalresp's own test data plus the synthetic
  fixtures, and 460 FAP/AP text comparisons (`--formats`, which includes phase unwrapping), with no
  mismatches. A further 260 comparisons on 26 current channels fetched from the EarthScope station
  service (8 networks) also matched, and the XML reader agreed with evalresp's own `xml2resp`
  converter on every channel.

Confirmed matching: Laplace (rad/s and Hz) and Z-transform poles/zeros, asymmetric/even/odd-symmetric
FIR (including continuation blockettes and delay correction), IIR coefficients, response lists, gain-only
stages, sensitivity-frequency renormalization, `dis`/`vel`/`acc`/`def` conversion including nm/mm/cm
inputs, `-ts`, `-use-estimated-delay` and stage ranges.

Deliberate differences (where this package is more exact than evalresp):

* **StationXML precision.** StationXML is read at full precision. evalresp converts it to RESP text first,
  rounding every coefficient to six significant digits, which can move values near deep stopband nulls
  (for example a FIR response at Nyquist). Where it matters, this package is the more exact of the two.
* **Exact unit symbols.** When you request `-u dis|vel|acc`, the documented input unit of the first stage
  decides whether and how the response is converted. evalresp matches only the *start* of the unit text,
  so `M/S/S` (acceleration) is read as velocity because it begins with `M/S`, and any unit beginning with
  `M` (`MBAR`, `MICROVOLTS`) is read as displacement; the conversion is then silently wrong by a factor of
  2*pi*f. This package matches the whole unit symbol (the text before the first space, hyphen, comma or
  parenthesis) against the known spellings: `M`, `M/S`, `M/SEC`, and the acceleration forms `M/S**2`,
  `M/S^2`, `M/S2`, `M/S/S` and their `SEC` equivalents, each with an optional `N`, `M` or `C` prefix.
  Anything else is unrecognized and the request fails with an "undefined units" error instead of a wrong
  answer. Results for `def` (the default) are not affected, and neither are responses that use the standard
  spellings.

## Limitations

* **Polynomial responses (blockette 62)** are implemented from evalresp's source, with unit tests of the
  formula, but could not be compared against the C program: the evalresp build used for validation crashes
  on files containing them.
* **`-il`/`-ii`** (spline interpolation of response-list output) is not implemented and is rejected
  rather than silently ignored. Without it, evalresp's default behavior (report at the listed
  frequencies) is reproduced.
* **RESP dictionary references** (blockette 60 and the blockette 4x lookups) are not supported and raise
  `ResponseParseError`.
* Analog (non-Z) coefficient filters are rejected, as in evalresp.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # ~280 tests, ~2 s
ruff check src tests tools
```

`tools/make_synthetic.py` and `tools/make_golden.py` regenerate the synthetic fixtures and the golden
reference results (the latter needs a built C evalresp). `tests/pyodide/` holds the WebAssembly smoke
test run in CI; see `.github/workflows/tests.yml`.

The package is Apache-2.0 (see `LICENSE`). The test data under `tests/data/real` comes from the evalresp project (LGPL-3.0); see `tests/data/README.md`.

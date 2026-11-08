# seismic-response

Evaluate seismological instrument responses specified as **StationXML**, **SEED RESP** or **SAC poles-and-zeros**
as selected frequencies to get complex-spectra or frequency-amplitude-phase results.  This package is a
superset of the capability of the legacy [evalresp](https://github.com/EarthScope/evalresp) program.

* **Python library**: pure Python, no dependencies, runs unchanged under [Pyodide](https://pyodide.org) (WebAssembly)
* **evalresp-compatible command line**: `pyevalresp` (with a few [deliberate differences](#deliberate-differences-from-evalresp))
* **interactive GUI**: a web app (plots amplitude and phase, per-stage curves, FDSN fetch, export) that runs
  locally, as a desktop window, or entirely in the browser with no server ([try it](https://earthscope-labs.github.io/seismic-response/app/))

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
messages go to stderr and a request matching no channel prints a warning. Also, `-u` defaults to `def`
(the units documented in the response) rather than evalresp's `vel`; pass `-u vel` to get evalresp's
behavior.

`seismic-response` is the umbrella command: `seismic-response info FILE [-s]` lists channels and stages,
`seismic-response evalresp ...` is `pyevalresp`, and `seismic-response gui [FILE ...]` starts the GUI.

## User Interface - Seismological Response Explorer

```sh
seismic-response gui RESP.IU.ANMO.00.BHZ       # opens your browser on a local server
seismic-response gui --app                     # native window (needs the [desktop] extra)
python -m seismic_response.gui --build site/   # static copy: host it anywhere, no Python server needed
```

The browser version of each release is published to GitHub Pages at
<https://earthscope-labs.github.io/seismic-response/app/>. A static copy has to be served over HTTP
(for example `python -m http.server -d site/`); opening `index.html` from disk does not work.

Load files by dropping them on the page, enter a file URL, or fetch from an FDSN station web service. Choose
an epoch of an FDSN Source ID from the list, then set the
frequency range, output unit and options, and tick the stages to include in the stage table. The plot shows
amplitude and phase with a hover read-out; individual stages can be overlaid; tick several epochs, of the same
or different Source IDs, to overlay them for comparison; results export as evalresp text
(FAP, AP, complex), SVG or PNG.

A link can open the page with files already loaded using the `url` parameter (repeatable, percent-encoded),
for example `.../app/?url=https%3A%2F%2Fexample.org%2Fresponse.xml`. The file server must allow cross-origin
requests.

The page talks to the same Python through a small JSON API (`seismic_response.web_api`). With the local
server that is plain HTTP on the loopback interface (requests with a foreign `Host` or `Origin` are
refused). On a static host it loads Pyodide from a CDN and runs the package in the browser.

## How closely does it match evalresp?

The numerical code is a port of evalresp's own logic (stage consistency checks, gain/normalization
handling, FIR/IIR/pole-zero evaluation, unit conversion, phase unwrapping) rather than a re-derivation.
It is checked against a built evalresp in two ways:

* **Golden tests** (`tests/test_golden.py`, run everywhere): 369 results captured from the C program
  (evalresp 5.1.0) for real and synthetic responses and a selection of evalresp's own test responses, over a
  matrix of options (units, `-ts`, estimated delay, stage ranges, `-il`, `-b62_x`), matched to within the
  7 digits it prints. Each point may also differ by up to 1e-14 of the response's peak (floating-point
  cancellation near a stopband), and points where both values are below 1e-9 of the peak (such as a FIR
  response above Nyquist) are numerical noise and are not compared.
* **Live comparison** (`tools/compare_evalresp.py BINARY FILES...`, needs a built C evalresp, 5.1.0):
  2,842 complex-spectrum comparisons over every response in evalresp's own test data and 381 over this
  repository's fixtures, and 952 FAP/AP text comparisons on evalresp's test data (`--formats`, which
  includes phase unwrapping), with no mismatches.

Confirmed matching: Laplace (rad/s and Hz) and Z-transform poles/zeros, asymmetric/even/odd-symmetric
FIR (including continuation blockettes and delay correction), IIR coefficients, response lists, gain-only
stages, sensitivity-frequency renormalization, `dis`/`vel`/`acc`/`def` conversion including nm/mm/cm
inputs, `-ts`, `-use-estimated-delay` and stage ranges.

### Deliberate differences from evalresp

* **Default output unit.** With no unit requested, responses are evaluated in the units documented in the
  response (equivalent to `def` in evalresp): counts per m/s for a velocity sensor, counts per m/s² for
  an accelerometer, counts per pascal for a pressure sensor, and so on. Any documented unit, or none, is
  accepted. This applies to the library (`evaluate()`), `pyevalresp`, the web API and the GUI. evalresp
  defaults to `vel`; pass `-u vel` (or `unit="vel"`) for that, subject to the next point.
* **No unconverted responses for evaluation in `dis`, `vel` or `acc`.** These require the first stage
  that documents input units to use a ground-motion unit: `M`, `M/S`, `M/SEC`, or an acceleration form (`M/S**2`,
  `M/S^2`, `M/S2`, `M/S/S` and their `SEC` equivalents), each with an optional `N`, `M` or `C` prefix. The
  unit symbol is the text before the first space, hyphen, comma or parenthesis, so a description is
  optional. Any other input unit, or none, fails with an error. evalresp makes an exception for pressure
  (`PA`, `KPA`), tesla (`T`, `NT`), degrees C, counts and volts inputs, and for responses with no
  documented input units: it returns them unconverted, silently for `vel` (its default) and with a
  warning for `dis` and `acc`. This package does not; use `def` for those responses.

## Limitations

* **Polynomial responses (blockette 62)** are evaluated at `b62_x` (default 0, any value allowed) as the
  first derivative of the MacLaurin polynomial; a negative slope is a negative real response, as in
  evalresp 5.1.0 and later.
* **Response-list interpolation** (`-il`, or `interpolate_list=True`) resamples a blockette 55 response
  at the requested frequencies with a natural cubic spline in log10(frequency), interpolating
  log10(amplitude) and the unwrapped phase. If any listed frequency or amplitude, or any requested
  frequency, is not positive it falls back to linear space with a warning. Requested frequencies outside
  the list's range are dropped with a warning, and the list frequencies must be strictly increasing.
  `-ii` is accepted as an alias. This matches evalresp releases that include the spline fixes; evalresp
  5.0.1 and earlier give different values for requested frequencies that are not listed, because they
  swap two spline coefficients (the result is not smooth on unevenly spaced frequencies) and interpolate in
  linear space, where steep roll-offs can overshoot or go negative.
* **RESP dictionary references** (blockette 60 and the blockette 4x lookups) are not supported and raise
  `ResponseParseError`.
* Analog (non-Z) coefficient filters are rejected, as in evalresp.

## References

* **SEED**: [SEED Reference Manual, SEED Format Version 2.4](https://www.fdsn.org/pdf/SEEDManual_V2.4.pdf)
  (FDSN), which defines the response blockettes that RESP files carry, and the
  [FDSN publications](https://www.fdsn.org/publications/) page.
* **evalresp**: the [evalresp repository](https://github.com/EarthScope/evalresp) and the
  [5.1.0 development version](https://github.com/EarthScope/evalresp/tree/develop) this package is validated against.
* **SAC poles and zeros**: Goldstein, P., Dodge, D., Firpo, M., and Minner, L. (2003). SAC2000: Signal
  processing and analysis tools for seismologists and engineers. In W. H. K. Lee, H. Kanamori, P. C. Jennings,
  and C. Kisslinger (Eds.), *International Handbook of Earthquake and Engineering Seismology*, Part B, ch. 85.5,
  pp. 1613–1614. Academic Press, London.
  [doi:10.1016/S0074-6142(03)80284-X](https://doi.org/10.1016/S0074-6142(03)80284-X)
* **Related FDSN standards** used by this package: [StationXML](https://docs.fdsn.org/projects/stationxml/en/latest/)
  and [Source Identifiers](https://docs.fdsn.org/projects/source-identifiers/en/latest/).

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # ~550 tests, ~2 s
ruff check src tests tools
```

`tools/make_synthetic.py` and `tools/make_golden.py` regenerate the synthetic fixtures and the golden
reference results (the latter needs a built C evalresp). `tests/pyodide/` holds the WebAssembly smoke
test run in CI; see `.github/workflows/tests.yml`.

The package is Apache-2.0 (see `LICENSE`).

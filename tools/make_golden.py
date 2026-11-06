#!/usr/bin/env python3
"""Capture reference results from the C evalresp program into tests/data/golden/golden.json.

    python tools/make_golden.py /path/to/evalresp/src/evalresp tests/data

Every fixture channel is evaluated by the C program for each case in
``compare_evalresp.CASES``; the test-suite then checks this package against the
stored numbers, so the C program is not needed to run the tests.
"""

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import compare_evalresp as ce

import seismic_response as sr


def main():
    binary, data = sys.argv[1], Path(sys.argv[2])
    entries = []
    fixtures = sorted(list((data / "real").glob("*")) + list((data / "synthetic").glob("*.resp")))
    with tempfile.TemporaryDirectory() as work:
        for path in fixtures:
            if path.name.endswith(".xml2resp.resp"):
                continue  # derived from the XML fixture; used for the structure test only
            source = ce.Source(str(path), binary)
            for index, chan in enumerate(source.channels):
                end = chan.end_time or (chan.start_time + timedelta(days=400))
                when = (chan.start_time + min((end - chan.start_time) / 2, timedelta(days=60))).replace(microsecond=0)
                has_list = any(isinstance(s.filter, sr.ResponseList) for s in chan.stages)
                for case in ce.CASES:
                    if case.get("il") and not has_list:
                        continue
                    rows, err = ce.run_c(binary, source, index, chan, when, case, work)
                    if rows is None:
                        continue  # C rejected it (e.g. stage range with no matching stage)
                    entries.append({
                        "fixture": str(path.relative_to(data)),
                        "nslc": chan.nslc,
                        "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "case": case,
                        "rows": rows,
                    })
    out = data / "golden" / "golden.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, separators=(",", ":")))
    print(f"wrote {len(entries)} reference results to {out} ({out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()

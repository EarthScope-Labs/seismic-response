// Smoke test: the package must import and give evalresp's numbers under Pyodide (WebAssembly).
//
//   python -m seismic_response.gui --build web-dist     # builds seismic_response.zip
//   cd tests/pyodide && npm install && node smoke.mjs ../../web-dist ../data/real
//
// Set PYODIDE_MODULE to a path to a local pyodide.mjs to avoid the npm package.
import fs from "node:fs";
import path from "node:path";

const [dist, dataDir] = process.argv.slice(2);
if (!dist || !dataDir) {
  console.error("usage: node smoke.mjs <built-gui-dir> <tests/data/real>");
  process.exit(2);
}

const { loadPyodide } = await import(process.env.PYODIDE_MODULE || "pyodide");
const py = await loadPyodide(process.env.PYODIDE_INDEX ? { indexURL: process.env.PYODIDE_INDEX } : {});

const zip = new Uint8Array(fs.readFileSync(path.join(dist, "seismic_response.zip")));
py.unpackArchive(zip, "zip", { extractDir: "/pkg" });
py.runPython("import sys\nsys.path.insert(0, '/pkg')");
const api = py.pyimport("seismic_response.web_api");
const call = (fn, ...args) => JSON.parse(api.call(fn, ...args));

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) failures++;
};
const close = (a, b, rel = 1e-6) => Math.abs(a - b) <= rel * Math.max(Math.abs(a), Math.abs(b));

console.log("Python", py.runPython("import sys; sys.version.split()[0]"));

// RESP: values computed by the C evalresp for this response (tests/data/golden)
const resp = fs.readFileSync(path.join(dataDir, "RESP.IU.ANMO.00.BHZ"), "utf8");
check("load RESP", call("load", "RESP.IU.ANMO.00.BHZ", resp).added === 2);

const req = {
  channel: 0,
  frequencies: { min: 0.01, max: 10, count: 3, spacing: "log" },
  unit: "vel",
  per_stage: true,
};
const res = call("evaluate", JSON.stringify(req));
check("evaluate ok", res.ok, res.error);
const expectedAmp = [6.504193e8, 9.881607e8, 2.402124e4];
check("amplitudes match C evalresp", res.amplitude.every((v, i) => close(v, expectedAmp[i])), JSON.stringify(res.amplitude));
check("per-stage curves", Object.keys(res.stage_values).length === 6);

const fap = call("export", JSON.stringify(req), "fap");
check("FAP text identical to C output", fap.text.split("\n")[0] === "1.000000E-02 6.504193E+08 5.353581E+01", fap.text.split("\n")[0]);

// StationXML parsing (xml.etree is part of the Pyodide standard library)
const xml = fs.readFileSync(path.join(dataDir, "station-3.xml"), "latin1");
const loaded = call("load", "station-3.xml", xml);
check("load StationXML", loaded.ok && loaded.added === 1, JSON.stringify(loaded));
check("errors are reported, not thrown", call("load", "bad.txt", "not a response").ok === false);

process.exit(failures ? 1 : 0);

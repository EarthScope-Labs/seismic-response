"""The JSON API behind the GUI, and the local server that exposes it."""

import http.client
import io
import json
import threading
import zipfile

import pytest
from conftest import DATA

from seismic_response import web_api
from seismic_response.gui import server

ANMO = (DATA / "real" / "RESP.IU.ANMO.00.BHZ").read_bytes()


@pytest.fixture(autouse=True)
def fresh_session():
    web_api.clear()
    yield
    web_api.clear()


def request(channel=0, **kw):
    req = {"channel": channel, "frequencies": {"min": 0.01, "max": 10, "count": 3, "spacing": "log"}, "unit": "vel"}
    req.update(kw)
    return json.dumps(req)


# ------------------------------------------------------------------ web_api


def test_load_list_and_select():
    assert json.loads(web_api.load("RESP.IU.ANMO.00.BHZ", ANMO)) == {"ok": True, "added": 2}
    chans = json.loads(web_api.channels())
    assert [c["id"] for c in chans] == [0, 1]
    first = chans[0]
    assert first["source_id"] == "FDSN:IU_ANMO_00_B_H_Z" and first["end"] == "2000-10-19T16:00:00Z"
    assert chans[1]["end"] is None
    assert first["stages"][0]["sequence"] == 1 and first["stages"][0]["gain"] == 2061.68
    assert json.loads(web_api.select("FDSN:IU_ANMO_*", "1999-06-01T00:00:00Z")) == [0]
    assert json.loads(web_api.select("FDSN:IU_ANMO_*", "2005-01-01T00:00:00Z")) == [1]
    assert json.loads(web_api.select("FDSN:XX_*", None)) == []


def test_evaluate_matches_known_values_and_is_json_safe():
    web_api.load("x", ANMO)
    res = json.loads(web_api.evaluate_response(request()))
    assert res["ok"] and res["frequencies"][0] == pytest.approx(0.01)
    assert res["amplitude"] == pytest.approx([650419272.37, 988160706.75, 24021.2399], rel=1e-7)
    assert res["phase"][0] == pytest.approx(53.53581, rel=1e-6)
    assert res["info"]["calculated_sensitivity"] == pytest.approx(864678843.1, rel=1e-9)
    json.dumps(res, allow_nan=False)  # no NaN/Infinity: the browser's JSON.parse would reject them


def test_per_stage_unwrap_and_stage_range():
    web_api.load("x", ANMO)
    res = json.loads(web_api.evaluate_response(request(per_stage=True, unwrap=True, stages=[1, 2])))
    assert sorted(res["stage_values"]) == ["1", "2"]
    assert [s["selected"] for s in res["info"]["stages"]][:3] == [True, True, False]


def test_errors_are_returned_not_raised():
    web_api.load("x", ANMO)
    bad = json.loads(web_api.evaluate_response(request(unit="parsecs")))
    assert not bad["ok"] and "Unit" in bad["error"]
    assert not json.loads(web_api.evaluate_response(request(channel=99)))["ok"]
    assert not json.loads(web_api.evaluate_response("not json"))["ok"]
    assert not json.loads(web_api.load("junk.txt", "nothing useful here"))["ok"]
    assert not json.loads(web_api.evaluate_response(request(stages=[40, 50])))["ok"]
    assert json.loads(web_api.call("nonsense"))["ok"] is False


def test_export_text_formats():
    web_api.load("x", ANMO)
    fap = json.loads(web_api.export_text(request(), "fap"))
    assert fap["filename"] == "FAP.IU.ANMO.00.BHZ" and fap["text"].splitlines()[0].startswith("1.000000E-02 6.504193E+08")
    ap = json.loads(web_api.export_text(request(), "ap"))
    assert "# AMP.IU.ANMO.00.BHZ" in ap["text"] and "# PHASE.IU.ANMO.00.BHZ" in ap["text"]
    cs = json.loads(web_api.export_text(request(), "cs"))
    assert cs["filename"].startswith("SPECTRA.")
    assert not json.loads(web_api.export_text(request(), "pdf"))["ok"]


# ------------------------------------------------------------------ server


@pytest.fixture
def live_server():
    srv = server._Server(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def http_call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type"), data


def test_server_serves_the_page_and_assets(live_server):
    for path, ctype in [("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")]:
        status, ct, body = http_call(live_server, "GET", path)
        assert status == 200 and ct.startswith(ctype) and body
    assert b"Seismic Response" in http_call(live_server, "GET", "/")[2]
    assert http_call(live_server, "GET", "/missing.js")[0] == 404
    assert http_call(live_server, "GET", "/../pyproject.toml")[0] == 404  # no path traversal
    assert http_call(live_server, "GET", "/%2e%2e/%2e%2e/etc/passwd")[0] == 404


def test_server_api_round_trip(live_server):
    assert json.loads(http_call(live_server, "GET", "/api/ping")[2])["ok"]
    body = json.dumps(["RESP.IU.ANMO.00.BHZ", ANMO.decode("utf-8")])
    status, _, data = http_call(live_server, "POST", "/api/load", body)
    assert status == 200 and json.loads(data) == {"ok": True, "added": 2}
    status, _, data = http_call(live_server, "POST", "/api/evaluate", json.dumps([request()]))
    assert json.loads(data)["ok"]
    # malformed bodies are answered, not crashed on
    assert json.loads(http_call(live_server, "POST", "/api/load", "{}")[2])["ok"] is False


def test_server_rejects_foreign_hosts_and_origins(live_server):
    """Protect the local API from DNS-rebinding and cross-site requests."""
    assert http_call(live_server, "GET", "/api/ping", headers={"Host": "evil.example"})[0] == 403
    assert http_call(live_server, "POST", "/api/clear", "[]", {"Origin": "https://evil.example"})[0] == 403
    assert http_call(live_server, "POST", "/api/clear", "[]", {"Origin": f"http://127.0.0.1:{live_server}"})[0] == 200


def test_server_refuses_to_bind_to_public_interfaces(capsys):
    assert server.serve(host="0.0.0.0", open_browser=False) == 2
    assert "loopback" in capsys.readouterr().err


def test_package_zip_for_pyodide_is_pure_python_without_gui():
    names = zipfile.ZipFile(io.BytesIO(server.package_zip())).namelist()
    assert "seismic_response/__init__.py" in names and "seismic_response/web_api.py" in names
    assert not any("/gui/" in n for n in names)
    assert all(n.endswith(".py") for n in names)


def test_static_build(tmp_path):
    server.build_static(tmp_path / "site")
    produced = {p.name for p in (tmp_path / "site").iterdir()}
    assert {"index.html", "app.js", "style.css", "seismic_response.zip", "version.json"} <= produced


def test_exclude_stages_and_version():
    web_api.load("x", ANMO)
    res = json.loads(web_api.evaluate_response(request(exclude_stages=[2])))
    assert res["ok"]
    flags = {s["sequence"]: s["selected"] for s in res["info"]["stages"]}
    assert flags[2] is False and flags[1] is True
    assert json.loads(web_api.call("version"))["version"]

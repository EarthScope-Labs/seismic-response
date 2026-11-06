"""Local server for the response GUI.

``seismic-response gui`` starts this server on the loopback interface and opens the
page in a browser (or in a native window when ``pywebview`` is installed).  The
page talks to this process over a small JSON API, so everything runs locally and
offline.  The very same page also works with no server at all: when no API is
found it loads Pyodide and runs this package inside the browser
(see :func:`build_static`).
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import sys
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from .. import __version__, web_api

STATIC_DIR = Path(__file__).parent / "static"
PACKAGE_DIR = Path(__file__).resolve().parent.parent

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
    ".json": "application/json",
    ".ico": "image/x-icon",
}

_MAX_BODY = 64 * 1024 * 1024  # largest accepted upload (StationXML files can be big)


def package_zip() -> bytes:
    """The pure-Python package (without the GUI) as a zip, for loading into Pyodide."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            rel = path.relative_to(PACKAGE_DIR)
            if rel.parts[0] == "gui":
                continue
            zf.write(path, (Path("seismic_response") / rel).as_posix())
    return buf.getvalue()


def build_static(dest: Path) -> None:
    """Write a self-contained static copy of the GUI (runs fully in-browser via Pyodide)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for f in STATIC_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
    (dest / "seismic_response.zip").write_bytes(package_zip())
    (dest / "version.json").write_text(json.dumps({"version": __version__}), encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = f"seismic-response/{__version__}"

    # Quiet by default
    def log_message(self, fmt, *args):  # noqa: D401
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        """Reject requests whose Host/Origin is not this server (DNS rebinding / CSRF guard)."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc in allowed

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        path = urlsplit(self.path).path
        if path == "/api/ping":
            return self._send(200, json.dumps({"ok": True, "version": __version__}).encode(), "application/json")
        if path == "/seismic_response.zip":
            return self._send(200, package_zip(), "application/zip")
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, target.read_bytes(), _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(400, b"bad length", "text/plain")
        if length > _MAX_BODY:
            return self._send(413, b"too large", "text/plain")
        try:
            args = json.loads(self.rfile.read(length) or b"[]")
            if not isinstance(args, list):
                raise ValueError("arguments must be a JSON array")
            with self.server.lock:  # the session store is shared
                result = web_api.call(path[len("/api/") :], *args)
        except Exception as exc:
            result = json.dumps({"ok": False, "error": f"Bad request: {exc}"})
        self._send(200, result.encode(), "application/json")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lock = threading.Lock()
        self.verbose = False


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    native: bool = False,
    files: Optional[List[str]] = None,
) -> int:
    """Run the GUI server until interrupted; returns a process exit code."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("error: the GUI server only binds to the loopback interface", file=sys.stderr)
        return 2

    web_api.clear()
    for name in files or []:
        try:
            data = Path(name).read_bytes()
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        res = json.loads(web_api.load(Path(name).name, data))  # type: ignore[arg-type]
        if not res.get("ok"):
            print(f"error: {name}: {res.get('error')}", file=sys.stderr)
            return 5

    server_class = _Server
    if host == "::1":
        server_class = type("_Server6", (_Server,), {"address_family": socket.AF_INET6})
    server = server_class((host, port), _Handler)
    url = f"http://{host if host != '::1' else '[::1]'}:{server.server_address[1]}/"
    print(f"seismic-response GUI at {url}  (Ctrl-C to quit)")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if native:
            try:
                import webview  # type: ignore
            except ImportError:
                print("pywebview is not installed (pip install 'seismic-response[desktop]'); "
                      "falling back to the browser", file=sys.stderr)
                native = False
            else:
                webview.create_window("Seismic Response", url, width=1280, height=860)
                webview.start()  # blocks until the window is closed
                return 0
        if open_browser and not native:
            webbrowser.open(url)
        while thread.is_alive():  # short joins keep Ctrl-C responsive on Windows
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0

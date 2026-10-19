"""Loopback-only static export fixtures for download UI integration tests."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[1] / "marketing" / "out"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, mode="normal", **kwargs):
        self.mode = mode
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *_):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.mode == "unavailable" and path in {"/downloads.json", "/python-package.json"}:
            self.send_error(503)
            return
        if path == "/downloads.json":
            catalog = json.loads((ROOT / "downloads.json").read_text())
            if self.mode == "invalid":
                row = dict(catalog["artifacts"][0])
                row["href"] = row["href"].rsplit("/", 1)[0] + "/different-file.exe"
                catalog["artifacts"] = [row]
            else:
                catalog["artifacts"][0]["built_at"] = "2026-10-14T12:30:00Z"
            raw = json.dumps(catalog).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        super().do_GET()


if __name__ == "__main__":
    servers = {mode: ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, mode=mode))
               for mode in ("normal", "unavailable", "invalid")}
    for server in servers.values():
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({mode: f"http://127.0.0.1:{server.server_port}" for mode, server in servers.items()}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers.values():
            server.shutdown()
            server.server_close()

"""Run the API (and, once it exists, the static page) locally, the way Vercel will.

    python scripts/dev_server.py            # http://127.0.0.1:8787
    python scripts/dev_server.py --port 9000

POST /api/chat goes to the real serverless handler; GET for any other path
serves files from web/. Reads keys from .env.
"""

import argparse
import json
import mimetypes
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.chat import handler as ApiHandler  # noqa: E402
from core import config  # noqa: E402

WEB = ROOT / "web"
# Apply the same security headers Vercel will, so CSP problems show up locally.
STATIC_HEADERS = [
    (h["key"], h["value"])
    for rule in json.loads((ROOT / "vercel.json").read_text()).get("headers", [])
    for h in rule["headers"]
]


class DevHandler(ApiHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            return super().do_GET()
        target = (WEB / path.lstrip("/")).resolve()
        if target.is_dir():
            target = target / "index.html"
        if WEB.resolve() not in target.parents or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        for key, value in STATIC_HEADERS:
            self.send_header(key, value)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?")[0] != "/api/chat":
            self.send_error(404)
            return
        super().do_POST()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    config.load_dotenv()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DevHandler)
    print(f"serving on http://127.0.0.1:{args.port}  (POST /api/chat)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

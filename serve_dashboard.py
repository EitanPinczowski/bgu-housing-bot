"""
Serve the dashboard live, so it's always current and you can open it on your phone.

    python serve_dashboard.py          # prints the URLs (token included) and serves
    python serve_dashboard.py --port 9000

The static `data/dashboard.html` can't do three things a phone needs: poll for new
listings, be opened over the network at all, or accept a vote. This serves the same page
from SQLite on every request instead.

SECURITY — read this before exposing it anywhere
------------------------------------------------
The page lists OTHER PEOPLE'S phone numbers and home addresses. Therefore:

  * A token is required on EVERY route. There is no unauthenticated mode. It comes from
    DASHBOARD_TOKEN in .env, else one is generated into data/dashboard_token.txt (which
    is gitignored) so a phone bookmark survives a restart. Compared with
    hmac.compare_digest; anything else gets a bare 403.
  * The image proxy will only fetch URLs that are ALREADY IN THE DATABASE. It is keyed
    by a hash of the stored URL, never by a URL from the query string — otherwise it
    would be an open relay for fetching arbitrary internal addresses.
  * Only two routes write, both through storage.py's existing helpers. No request input
    is ever concatenated into SQL (filtering happens in the browser, and the text search
    is a Python substring test, not a LIKE).
  * There are exactly the routes in _ROUTES. No filesystem serving, no directory listing.

For access away from home use **Tailscale** — a private network between your own devices
— rather than a public tunnel or a router port-forward. Traffic inside a tailnet is
already WireGuard-encrypted, and no public URL exists to be found or indexed.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import dashboard
import storage

_TOKEN_FILE = config.DATA_DIR / "dashboard_token.txt"
_img_map_lock = threading.Lock()
_img_map: dict = {}          # sha1(url) -> url, rebuilt from the DB


# --- auth ------------------------------------------------------------------------
def token() -> str:
    """The shared secret. .env first (the project's established secret location), else a
    generated one persisted so a phone bookmark keeps working across restarts."""
    from_env = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if from_env:
        return from_env
    try:
        existing = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    fresh = secrets.token_urlsafe(24)
    config.DATA_DIR.mkdir(exist_ok=True)
    _TOKEN_FILE.write_text(fresh, encoding="utf-8")
    return fresh


def _authorized(query: dict, headers) -> bool:
    supplied = (query.get("token", [""])[0]
                or (headers.get("X-Dashboard-Token") if headers else "") or "")
    return hmac.compare_digest(supplied, token())


# --- the image cache: FB urls expire, so keep a copy the first time we see one -----
def _image_urls() -> dict:
    """{sha1(url): url} for every image URL stored on a listing. This map IS the
    allow-list — a hash that isn't in it is never fetched."""
    global _img_map
    with _img_map_lock:
        rows = storage.get_all_images() if hasattr(storage, "get_all_images") else []
        fresh = {}
        for url in rows:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                fresh[hashlib.sha1(url.encode()).hexdigest()] = url
        _img_map = fresh
        return _img_map


def _cached_image(digest: str):
    """(bytes, content_type) for a listing image, fetching and caching it once. None if
    the hash isn't one of ours, or the fetch fails."""
    config.DASHBOARD_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DASHBOARD_IMAGE_DIR / digest
    if path.exists():
        return path.read_bytes(), mimetypes.guess_type(str(path))[0] or "image/jpeg"
    url = _img_map.get(digest) or _image_urls().get(digest)
    if not url:
        return None                       # not a URL we stored -> never fetch it
    try:
        import requests
        r = requests.get(url, timeout=15, stream=True)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if not ctype.startswith("image/"):
            return None
        blob = r.raw.read(config.DASHBOARD_MAX_IMAGE_BYTES + 1, decode_content=True)
        if len(blob) > config.DASHBOARD_MAX_IMAGE_BYTES:
            return None
        path.write_bytes(blob)
        return blob, ctype
    except Exception:
        return None                       # an expired FB url is normal, not an error


# --- request handling ------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "bgu-dashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # quiet; the console shows the URLs only
        pass

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # the page is private data; don't let a proxy or the browser cache it broadly
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _deny(self):
        self._send(403, b"forbidden")

    # -- verbs
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        if not _authorized(query, self.headers):
            return self._deny()

        if route == "/":
            return self._send(200, dashboard.render_live().encode("utf-8"),
                              "text/html; charset=utf-8")
        if route == "/api/version":
            return self._json(200, storage.dashboard_version())
        if route == "/api/listings.json":
            return self._json(200, {"listings": dashboard.rows_for_api(),
                                    "version": storage.dashboard_version()})
        if route.startswith("/img/"):
            digest = route[len("/img/"):]
            if not digest.isalnum() or len(digest) != 40:
                return self._send(404, b"not found")
            got = _cached_image(digest)
            if not got:
                return self._send(404, b"not found")
            blob, ctype = got
            return self._send(200, blob, ctype)
        return self._send(404, b"not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        if not _authorized(query, self.headers):
            return self._deny()
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})
        if route == "/api/route":            # takes a list, not a single key
            return self._route_plan(payload)
        key = (payload.get("key") or "").strip()
        if not key:
            return self._json(400, {"error": "missing key"})

        if route == "/api/mark":
            mark = payload.get("mark")
            if mark not in ("saved", "dismissed", "contacted"):
                return self._json(400, {"error": "bad mark"})
            # same ledger and same one-vote-per-user rule as the Telegram buttons
            recorded = storage.set_mark(key, payload.get("user") or "dashboard", mark)
            return self._json(200, {"ok": True, "recorded": bool(recorded),
                                    "score": storage.effective_score(
                                        key, storage.base_score(key))})
        if route == "/api/note":
            storage.set_note(key, payload.get("text") or "")
            return self._json(200, {"ok": True, "text": storage.get_note(key)})
        if route == "/api/walk":
            # POST, not GET /api/walk/<key>, because a dedup_key is phone|address —
            # a GET would write a landlord's phone number into the URL and the log.
            return self._json(200, dashboard.walk_route(key))
        return self._send(404, b"not found")

    def _route_plan(self, payload):
        keys = [k for k in (payload.get("keys") or []) if isinstance(k, str)][:8]
        return self._json(200, dashboard.plan_route(keys))


# --- startup ---------------------------------------------------------------------
def _lan_ip() -> str:
    """This machine's LAN address, without sending anything (UDP connect just picks a
    route). Falls back to localhost."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _tailscale_ip():
    """The 100.x tailnet address if Tailscale is up, else None."""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("100."):
                return ip
    except Exception:
        pass
    return None


def main() -> int:
    port = config.DASHBOARD_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    tok = token()
    _image_urls()                                    # warm the allow-list

    print(f"BGU dashboard on port {port} — token required on every request\n")
    print(f"  this machine : http://127.0.0.1:{port}/?token={tok}")
    print(f"  same Wi-Fi   : http://{_lan_ip()}:{port}/?token={tok}")
    ts = _tailscale_ip()
    if ts:
        print(f"  Tailscale    : http://{ts}:{port}/?token={tok}   <- use this from anywhere")
    else:
        print("  Tailscale    : not detected (install it for access away from home;\n"
              "                 prefer it over a public tunnel — see the README)")
    print("\n  Ctrl-C to stop.")

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")

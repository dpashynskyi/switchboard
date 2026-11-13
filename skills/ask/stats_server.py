"""Authenticated shared stats HTTP transport."""
import base64
import binascii
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import urllib.parse

from stats_store import MAX_BODY, StatsStore


def make_server(directory, read_token, ingest_token=None, host="127.0.0.1", port=8765, store=None):
    if not read_token or len(read_token) < 16 or (ingest_token is not None and len(ingest_token) < 16):
        raise ValueError("Tokens must be at least 16 characters")
    if ingest_token and hmac.compare_digest(read_token.encode(), ingest_token.encode()):
        raise ValueError("Read and ingest tokens must differ")
    store = store if store is not None else StatsStore(directory)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body=b"", kind="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="switchboard"')
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def auth(self, write=False):
            token = ""
            try:
                scheme, value = self.headers.get("Authorization", "").split(" ", 1)
                if scheme.lower() == "bearer":
                    token = value
                elif scheme.lower() == "basic":
                    token = base64.b64decode(value, validate=True).decode("utf-8").split(":", 1)[1]
            except (ValueError, IndexError, UnicodeError, binascii.Error):
                pass
            read = hmac.compare_digest(token.encode(), read_token.encode())
            ingest = bool(ingest_token) and hmac.compare_digest(token.encode(), ingest_token.encode())
            if not (read or ingest):
                self.respond(401)
                return False
            if not (ingest if write else read):
                self.respond(403)
                return False
            return True

        def do_GET(self):
            from dashboard import finite, page_html
            if not self.auth():
                return
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/":
                self.respond(200, page_html(server_mode=True).encode("utf-8"), "text/html; charset=utf-8")
            elif parts.path == "/api/state":
                machine = urllib.parse.parse_qs(parts.query).get("machine", ["all"])[0]
                try:
                    state = store.get(machine)
                    self.respond(200, json.dumps(finite(state), allow_nan=False).encode("utf-8"))
                except ValueError:
                    self.respond(400)
                except Exception:
                    self.respond(503)
            else:
                self.respond(404)

        def do_POST(self):
            if not self.auth(write=True):
                return
            if urllib.parse.urlsplit(self.path).path != "/ingest":
                self.respond(404)
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length") is None:
                self.respond(411)
                return
            try:
                length = int(self.headers["Content-Length"])
            except ValueError:
                self.respond(400)
                return
            if length < 0:
                self.respond(400)
                return
            if length > MAX_BODY:
                self.respond(413)
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self.respond(415)
                return
            try:
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError()
                result = store.ingest(json.loads(raw.decode("utf-8")))
                self.respond(200, json.dumps(result).encode())
            except (ValueError, UnicodeError, RecursionError):
                self.respond(400)
            except OSError:
                self.respond(500)

        def do_HEAD(self):
            if self.auth():
                self.respond(405)

        def do_PUT(self):
            if self.auth(write=True):
                self.respond(405)

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        def __getattr__(self, name):
            # BaseHTTPRequestHandler otherwise returns 501 before authenticating unknown methods.
            if name.startswith("do_"):
                return self.do_HEAD
            raise AttributeError(name)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    return server

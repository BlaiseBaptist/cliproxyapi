"""Edge shim in front of CLIProxyAPI for Codex /v1/responses traffic.

CLIProxyAPI refuses requests locally while a credential is in quota cooldown,
answering 429 {"error":{"code":"model_cooldown",...}}. Codex only stops on
error.type == "usage_limit_reached"; anything else is a retryable 429, so it
sits on "retrying provider". This shim rewrites that local quota refusal into
the OpenAI usage-limit shape and streams everything else through unchanged.
Transient cooldowns (no usage_limit_reached) pass through so Codex retries.

Also wraps bare SSE cyber_policy errors in response.failed. Codex 0.167.0
ignores the bare form and retries an apparent disconnect; response.failed
preserves the upstream refusal and makes it terminal.
"""
import http.client
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM = urlsplit(os.environ.get("UPSTREAM", "http://cliproxyapi:8317"))
# cliproxy-dash records quota windows and caches quota reads; the patched panel
# reaches both here, on its own origin, with its management key (checked there).
DASH = urlsplit(os.environ.get("DASH_UPSTREAM", "http://cliproxy-dash:8318"))
QUOTA_HISTORY_PATHS = {"/v0/management/quota-history", "/v8/management/quota-history"}
# The panel's quota cards read these through the management api-call endpoint.
# cliproxy-dash caches them (one upstream read per TTL shared by every reader),
# so those GETs go there; everything else, including POSTs, goes to the proxy.
API_CALL_PATHS = {"/v0/management/api-call", "/v8/management/requests/api-call"}
CACHED_QUOTA_URLS = {
    "https://api.anthropic.com/api/oauth/usage",
    "https://api.anthropic.com/api/oauth/profile",
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
    "https://chatgpt.com/backend-api/subscriptions",
}


def cached_quota_read(route, body):
    if route not in API_CALL_PATHS:
        return False
    try:
        req = json.loads(body)
    except ValueError:
        return False
    return (isinstance(req, dict) and (req.get("method") or "GET").upper() == "GET"
            and req.get("url") in CACHED_QUOTA_URLS)


LISTEN_HOST, LISTEN_PORT = os.environ.get("LISTEN", "0.0.0.0:8319").rsplit(":", 1)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
       "accept-encoding"}


def rewrite_cooldown(body: bytes):
    try:
        err = json.loads(body)["error"]
    except Exception:
        return None
    if not isinstance(err, dict) or err.get("code") != "model_cooldown":
        return None
    detail = f'{err.get("last_upstream_error", "")} {err.get("message", "")}'
    if "usage_limit_reached" not in detail:
        return None
    secs = int(err.get("reset_seconds") or 0)
    return json.dumps({"error": {
        "type": "usage_limit_reached",
        "message": "The usage limit has been reached",
        "resets_in_seconds": secs,
        "resets_at": int(time.time()) + secs,
        "proxy_code": "model_cooldown",
        "proxy_message": err.get("message", ""),
    }}).encode()


def responses_sse(resp):
    """Preserve SSE frames, normalizing only bare cyber-policy refusals."""
    frame = []
    response = {}
    for line in resp:
        frame.append(line)
        if line not in (b"\n", b"\r\n"):
            continue
        data = b"\n".join(l[5:].strip() for l in frame if l.startswith(b"data:"))
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            event = None
        if isinstance(event, dict):
            if event.get("type") == "response.created":
                value = event.get("response")
                if isinstance(value, dict):
                    response = value
            error = event.get("error")
            if (event.get("type") == "error" and isinstance(error, dict)
                    and error.get("code") == "cyber_policy"):
                failed = {**response, "status": "failed", "error": error}
                event = {**event, "type": "response.failed", "response": failed}
                del event["error"]
                yield b"event: response.failed\ndata: " + json.dumps(event).encode() + b"\n\n"
                print("normalized cyber_policy -> response.failed", flush=True)
                frame = []
                continue
        yield b"".join(frame)
        frame = []
    if frame:
        yield b"".join(frame)


DB_PATH = os.environ.get("EDGE_DB", "/data/requests.sqlite3")
REDACT = {"authorization", "proxy-authorization", "cookie", "set-cookie",
          "x-api-key", "api-key", "apikey", "x-auth-token", "x-access-token"}


def safe_headers(headers):
    return json.dumps([(k, "[REDACTED]" if (k.lower() in REDACT or
                       "api-key" in k.lower() or "token" in k.lower() or
                       "secret" in k.lower()) else v) for k, v in headers])


class Recorder:
    """Each operation is committed independently; logging never breaks proxying."""
    def __init__(self, handler):
        self.id = None
        self.seq = {"request": 0, "response": 0}
        self.run(lambda c: self.start(c, handler))

    def run(self, action):
        try:
            with sqlite3.connect(DB_PATH, timeout=2) as c:
                c.execute("PRAGMA busy_timeout=2000")
                action(c)
        except Exception as exc:
            # Never print exception details: paths, headers or SQL may be sensitive.
            print("edge database error: " + type(exc).__name__, file=sys.stderr, flush=True)

    def start(self, c, h):
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL, method TEXT, path TEXT, request_headers TEXT, status INTEGER, response_headers TEXT, outcome TEXT, error TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS body_chunks (request_id INTEGER NOT NULL, direction TEXT NOT NULL, seq INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(request_id,direction,seq)) WITHOUT ROWID")
        self.id = c.execute("INSERT INTO requests(started,method,path,request_headers) VALUES(?,?,?,?)", (time.time(), h.command, h.path, safe_headers(h.headers.items()))).lastrowid

    def chunk(self, direction, data):
        if self.id is None or not data:
            return
        seq = self.seq[direction]
        self.seq[direction] += 1
        self.run(lambda c: c.execute("INSERT INTO body_chunks VALUES(?,?,?,?)", (self.id, direction, seq, data)))

    def headers(self, status, headers):
        if self.id is not None:
            self.run(lambda c: c.execute("UPDATE requests SET status=?,response_headers=? WHERE id=?", (status, safe_headers(headers), self.id)))

    def finish(self, outcome, error=None):
        if self.id is not None:
            self.run(lambda c: c.execute("UPDATE requests SET finished=?,outcome=?,error=? WHERE id=?", (time.time(), outcome, error, self.id)))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self, log):
        out = bytearray()
        def read(n):
            while n:
                part = self.rfile.read1(min(n, 65536))
                if not part:
                    raise ConnectionError("incomplete request")
                out.extend(part)
                log.chunk("request", part)
                n -= len(part)
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                read(size)
                if self.rfile.read(2) != b"\r\n":
                    raise ValueError("invalid chunk")
        else:
            read(int(self.headers.get("Content-Length") or 0))
        return bytes(out)

    def _proxy(self):
        log = Recorder(self)
        conn = None
        sent_headers = False
        outcome, error = "complete", None
        try:
            body = self._read_body(log)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            target, path = UPSTREAM, self.path
            route, _, query = self.path.partition("?")
            if route in QUOTA_HISTORY_PATHS:
                target = DASH
                path = "/v0/management/quota-history" + ("?" + query if query else "")
            elif self.command == "POST" and cached_quota_read(route, body):
                target, path = DASH, "/v0/management/api-call"
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=3600)
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            out_headers = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP]
            if resp.status == 429:
                data = resp.read()
                new = rewrite_cooldown(data)
                if new is not None:
                    data = new
                    print("normalized model_cooldown -> usage_limit_reached", flush=True)
                out_headers.append(("Content-Length", str(len(data))))
                log.headers(resp.status, out_headers)
                self.send_response(resp.status)
                for k, v in out_headers:
                    self.send_header(k, v)
                self.end_headers()
                sent_headers = True
                log.chunk("response", data)
                self.wfile.write(data)
                self.wfile.flush()
                return
            length = resp.getheader("Content-Length")
            normalize_sse = (self.path.split("?", 1)[0] == "/v1/responses"
                             and resp.status == 200
                             and resp.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "text/event-stream")
            fixed = length is not None and not normalize_sse
            out_headers.append(("Content-Length", length) if fixed else ("Transfer-Encoding", "chunked"))
            log.headers(resp.status, out_headers)
            self.send_response(resp.status)
            for k, v in out_headers:
                self.send_header(k, v)
            self.end_headers()
            sent_headers = True
            chunks = responses_sse(resp) if normalize_sse else iter(lambda: resp.read1(65536), b"")
            received = 0
            for chunk in chunks:
                received += len(chunk)
                log.chunk("response", chunk)
                self.wfile.write(chunk if fixed else b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            if fixed and self.command != "HEAD" and received != int(length):
                raise ConnectionError("incomplete upstream response")
            if not fixed:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            outcome, error = "disconnect", type(exc).__name__
            self.close_connection = True
        except Exception as exc:
            outcome, error = "error", type(exc).__name__
            print("edge proxy error: " + error, file=sys.stderr, flush=True)
            self.close_connection = True
            if not sent_headers:
                try:
                    msg = b'{"error":{"type":"server_error","message":"edge upstream error"}}'
                    hdrs = [("Content-Type", "application/json"), ("Content-Length", str(len(msg)))]
                    log.headers(502, hdrs)
                    self.send_response(502)
                    for k, v in hdrs:
                        self.send_header(k, v)
                    self.end_headers()
                    log.chunk("response", msg)
                    self.wfile.write(msg)
                except Exception:
                    pass
        finally:
            if conn is not None:
                conn.close()
            log.finish(outcome, error)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _proxy


if __name__ == "__main__":
    os.umask(0o077)
    ThreadingHTTPServer.daemon_threads = True
    print(f"edge listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM.geturl()}", flush=True)
    ThreadingHTTPServer((LISTEN_HOST, int(LISTEN_PORT)), Handler).serve_forever()

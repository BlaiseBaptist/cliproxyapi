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
import base64
import http.client
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing
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


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL, method TEXT, path TEXT, request_headers TEXT, status INTEGER, response_headers TEXT, outcome TEXT, error TEXT)",
    "CREATE TABLE IF NOT EXISTS body_chunks (request_id INTEGER NOT NULL, direction TEXT NOT NULL, seq INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(request_id,direction,seq)) WITHOUT ROWID",
)
REQUEST_COLUMNS = {"finished", "method", "path", "request_headers", "status", "response_headers", "outcome", "error"}


class Writer:
    """The only code that writes the traffic database.

    Proxy threads call put() and never wait on SQLite: a single background
    thread commits queued records in batches. While the database is locked
    (a migration, an import, a backup) it keeps retrying and the queue grows.
    Past MAX_QUEUE_BYTES, records spill to a spool file next to the database,
    replayed once writing resumes. Shutdown spools whatever is still unwritten.

    Every record is an upsert keyed by (request id) or (request id, direction,
    seq), so records may be written in any order and replayed safely.
    """
    BATCH = 500
    MAX_QUEUE_BYTES = int(os.environ.get("EDGE_MAX_QUEUE_BYTES", str(64 << 20)))

    def __init__(self, path):
        self.path = path
        self.spool = path + ".spool"
        self.cond = threading.Condition()
        self.queue, self.queued_bytes = [], 0
        self.inflight = []
        self.stopping = False
        self.next_id = self.max_id() + 1
        self.thread = threading.Thread(target=self.loop, name="edge-db-writer", daemon=True)

    def max_id(self):
        top = 0
        try:  # a WAL read; it doesn't wait for writers
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)) as c:
                top = c.execute("SELECT MAX(id) FROM requests").fetchone()[0] or 0
        except sqlite3.Error:
            pass  # new database
        for name in self.spool_files():
            for rec in self.read_spool(name):
                top = max(top, rec[1])
        return top

    def new_id(self):
        with self.cond:
            self.next_id += 1
            return self.next_id - 1

    @staticmethod
    def size(rec):
        return 64 + sum(len(v) for v in rec if isinstance(v, (bytes, str))) + (
            sum(len(str(v)) for v in rec[3].values()) if rec[0] == "request" else 0)

    def put(self, rec):
        size = self.size(rec)
        with self.cond:
            if self.queued_bytes + size > self.MAX_QUEUE_BYTES:
                self.append_spool([rec])
                return
            self.queue.append(rec)
            self.queued_bytes += size
            self.cond.notify()

    # --- spool ------------------------------------------------------------

    def spool_files(self):
        folder, base = os.path.split(self.spool)
        try:
            names = os.listdir(folder or ".")
        except OSError:
            return []
        return sorted(os.path.join(folder, n) for n in names if n.startswith(os.path.basename(base)))

    def append_spool(self, recs):
        with open(self.spool, "a", encoding="ascii") as f:
            for rec in recs:
                f.write(json.dumps([base64.b64encode(v).decode() if isinstance(v, bytes) else v for v in rec]) + "\n")

    @staticmethod
    def read_spool(name):
        with open(name, encoding="ascii") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line cut short by a crash
                if rec[0] == "chunk":
                    rec[4] = base64.b64decode(rec[4])
                yield rec

    def take_spool(self):
        """Move the spool aside for replay; new spills start a fresh file."""
        with self.cond:
            if os.path.exists(self.spool):
                os.replace(self.spool, f"{self.spool}.{time.time_ns()}")
        return [n for n in self.spool_files() if n != self.spool]

    # --- writing ----------------------------------------------------------

    @staticmethod
    def apply(c, rec):
        if rec[0] == "chunk":
            c.execute("INSERT OR IGNORE INTO body_chunks VALUES(?,?,?,?)", rec[1:])
            return
        _, rid, started, fields = rec
        cols = [k for k in fields if k in REQUEST_COLUMNS]
        c.execute(
            f"INSERT INTO requests(id,started{''.join(',' + k for k in cols)}) VALUES(?,?{',?' * len(cols)})"
            f" ON CONFLICT(id) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in cols) or 'id=id'}",
            [rid, started, *(fields[k] for k in cols)])

    def commit(self, recs):
        with closing(sqlite3.connect(self.path, timeout=5)) as c:
            c.execute("PRAGMA busy_timeout=5000")
            with c:
                for rec in recs:
                    self.apply(c, rec)

    def write(self, recs):
        """Commit recs, retrying until it works or the edge stops. False if stopped."""
        delay = 0.5
        while True:
            try:
                self.commit(recs)
                return True
            except sqlite3.Error as exc:
                print("edge database busy, retrying: " + type(exc).__name__, file=sys.stderr, flush=True)
            if self.stopping:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 10)

    def setup(self):
        while not self.stopping:
            try:
                with closing(sqlite3.connect(self.path, timeout=5)) as c:
                    c.execute("PRAGMA journal_mode=WAL")
                    for sql in SCHEMA:
                        c.execute(sql)
                    c.commit()
                return True
            except sqlite3.Error as exc:
                print("edge database setup busy, retrying: " + type(exc).__name__, file=sys.stderr, flush=True)
                time.sleep(2)
        return False

    def loop(self):
        if not self.setup():
            return
        while not self.stopping:
            for name in self.take_spool():
                recs = list(self.read_spool(name))
                for i in range(0, len(recs), self.BATCH):
                    if not self.write(recs[i:i + self.BATCH]):
                        return
                os.remove(name)
            with self.cond:
                while not self.queue and not self.stopping and not os.path.exists(self.spool):
                    self.cond.wait(1)
                self.inflight, self.queue = self.queue[:self.BATCH], self.queue[self.BATCH:]
                self.queued_bytes -= sum(self.size(r) for r in self.inflight)
            if self.inflight and not self.write(self.inflight):
                return
            self.inflight = []

    def start(self):
        self.thread.start()

    def stop(self, wait=3):
        """Write what can be written quickly, then spool the rest."""
        deadline = time.time() + wait
        while time.time() < deadline:
            with self.cond:
                if not self.queue and not self.inflight:
                    break
            time.sleep(0.05)
        with self.cond:
            self.stopping = True
            self.cond.notify_all()
        self.thread.join(timeout=5)  # Docker allows 10 s after SIGTERM
        with self.cond:
            rest = self.inflight + self.queue
            self.inflight, self.queue = [], []
            if rest:
                self.append_spool(rest)


writer = None  # set in main; tests may install their own


class Recorder:
    """Queues one request's records; logging never slows or breaks proxying."""
    def __init__(self, handler):
        self.seq = {"request": 0, "response": 0}
        self.id, self.started = None, time.time()
        if writer is None:
            return
        self.id = writer.new_id()
        self.put({"method": handler.command, "path": handler.path,
                  "request_headers": safe_headers(handler.headers.items())})

    def put(self, fields):
        writer.put(("request", self.id, self.started, fields))

    def chunk(self, direction, data):
        if self.id is None or not data:
            return
        seq = self.seq[direction]
        self.seq[direction] += 1
        writer.put(("chunk", self.id, direction, seq, bytes(data)))

    def headers(self, status, headers):
        if self.id is not None:
            self.put({"status": status, "response_headers": safe_headers(headers)})

    def finish(self, outcome, error=None):
        if self.id is not None:
            self.put({"finished": time.time(), "outcome": outcome, "error": error})


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
    writer = Writer(DB_PATH)
    writer.start()
    server = ThreadingHTTPServer((LISTEN_HOST, int(LISTEN_PORT)), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"edge listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM.geturl()}", flush=True)
    try:
        server.serve_forever()
    finally:
        writer.stop()

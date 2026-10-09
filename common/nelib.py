"""NordEvent Lab - ühised abifunktsioonid (ainult Pythoni standardteek).

Iga teenus kasutab sama logivormingut (JSON-rida stdout'i), sama päringu
korrelatsioonitunnust (X-Request-ID) ja sama lihtsat HTTP-serverit.
"""
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("SERVICE_NAME", "service")
# Piiratud töölõimede kogum (rakendusserveri "worker pool"). 0 = piiramatu.
# NE-Classicus jagavad kataloog ja broneerimine sama kogumit.
_POOL_SIZE = int(os.environ.get("SERVER_WORKERS", "0") or 0)
_POOL = threading.BoundedSemaphore(_POOL_SIZE) if _POOL_SIZE > 0 else None
INSTANCE = socket.gethostname()
_log_lock = threading.Lock()


# ---------------------------------------------------------------- logimine
def now_iso():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


def log(level, event, rid=None, **fields):
    rec = {"ts": now_iso(), "level": level, "service": SERVICE, "instance": INSTANCE, "event": event}
    if rid:
        rec["request_id"] = rid
    rec.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(rec, ensure_ascii=False)
    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ------------------------------------------------------------ konfiguratsioon
def env_str(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and (v is None or str(v).strip() == ""):
        log("ERROR", "config_missing", variable=name,
            message=f"Kohustuslik keskkonnamuutuja {name} puudub - teenus ei käivitu")
        sys.exit(1)
    return v


def env_int(name, default):
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        log("ERROR", "config_invalid", variable=name, value=str(raw),
            message=f"{name} peab olema täisarv")
        sys.exit(1)


def env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "on", "yes", "jah")


def read_secret(name):
    """Saladus failist (<NAME>_FILE) või keskkonnamuutujast (<NAME>).

    Tagastab (väärtus, allikas). Keskkonnamuutuja kasutamine logitakse hoiatusena.
    """
    env_val = os.environ.get(name)
    if env_val:
        log("WARN", "secret_from_environment", secret=name,
            message="Saladus on keskkonnamuutujas - see on nähtav 'docker inspect' väljundis")
        return env_val, "environment"
    path = os.environ.get(name + "_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip(), "file"
    return None, "missing"


# --------------------------------------------------------------- HTTP klient
class UpstreamTimeout(Exception):
    pass


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method, url, body=None, headers=None, timeout=5.0):
    """Teeb JSON-päringu. Tagastab (status, data). Ajalõpul tõstab UpstreamTimeout,
    ühenduse puudumisel ConnectionError."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        if v is not None:
            req.add_header(k, str(v))
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            d = json.loads(raw) if raw else None
        except ValueError:
            d = {"raw": raw.decode(errors="replace")[:200]}
        return e.code, d
    except (socket.timeout, TimeoutError) as e:
        raise UpstreamTimeout(str(e))
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise UpstreamTimeout(str(e.reason))
        raise ConnectionError(str(e.reason))
    except (ConnectionResetError, http.client.HTTPException) as e:
        raise ConnectionError(repr(e))


# ------------------------------------------------------------- mõõtmine
class Latency:
    def __init__(self, size=2000):
        self._d = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, ms):
        with self._lock:
            self._d.append(ms)

    def pct(self, p):
        with self._lock:
            vals = sorted(self._d)
        if not vals:
            return None
        k = min(len(vals) - 1, int(round(p / 100.0 * (len(vals) - 1))))
        return round(vals[k])

    def summary(self):
        return {"p50": self.pct(50), "p95": self.pct(95), "n": len(self._d)}


# --------------------------------------------------------------- HTTP server
class HttpError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class Request:
    def __init__(self, method, path, query, body, headers, rid):
        self.method, self.path, self.query, self.body, self.headers, self.rid = \
            method, path, query, body, headers, rid


class App:
    def __init__(self):
        self.routes = []
        self.started = time.time()

    def route(self, method, pattern):
        rx = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn):
            self.routes.append((method, rx, fn))
            return fn
        return deco

    def serve(self, port=8000):
        port = int(os.environ.get("PORT", port))
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self, method):
                t0 = time.perf_counter()
                rid = self.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
                path, _, qs = self.path.partition("?")
                query = dict(urllib.parse.parse_qsl(qs))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = None
                queued_ms = None
                if _POOL is not None and path != "/health":
                    tq = time.perf_counter()
                    _POOL.acquire()
                    queued_ms = round((time.perf_counter() - tq) * 1000)
                try:
                    status, payload = self._dispatch(method, path, query, body, rid)
                finally:
                    if queued_ms is not None:
                        _POOL.release()
                self._respond(status, payload, rid, path, method, t0, queued_ms)

            def _dispatch(self, method, path, query, body, rid):
                status, payload = 404, {"error": "not_found", "path": path}
                for m, rx, fn in app.routes:
                    mt = rx.match(path)
                    if mt and m == method:
                        req = Request(method, path, query, body, self.headers, rid)
                        try:
                            status, payload = fn(req, **mt.groupdict())
                        except HttpError as e:
                            status, payload = e.status, dict({"error": e.message}, **e.extra)
                        except Exception as e:  # noqa: BLE001
                            log("ERROR", "unhandled_exception", rid, route=path, error=repr(e))
                            status, payload = 500, {"error": "internal_error"}
                        break
                return status, payload

            def _respond(self, status, payload, rid, path, method, t0, queued_ms):
                out = json.dumps(payload, ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(out)))
                    self.send_header("X-Request-ID", rid)
                    self.send_header("X-Served-By", INSTANCE)
                    self.end_headers()
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    log("WARN", "client_disconnected", rid, route=path,
                        message="Klient katkestas ühenduse enne vastust (nt ajalõpp)")
                dur = round((time.perf_counter() - t0) * 1000)
                if path != "/health":
                    log("INFO" if status < 500 else "ERROR", "request_end", rid, method=method,
                        route=path, http_status=status, duration_ms=dur,
                        waited_for_worker_ms=queued_ms)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        ThreadingHTTPServer.request_queue_size = 256
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        srv.daemon_threads = True
        log("INFO", "service_started", port=port, worker_pool=_POOL_SIZE or "piiramatu")
        srv.serve_forever()

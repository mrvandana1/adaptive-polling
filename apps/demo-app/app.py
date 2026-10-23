"""Demo workload (stdlib only). Everything interesting is changeable at runtime via /admin/config.

Metrics contract (what the controller reads) - keep these if you swap the app:
  http_requests_total{method,path,status}      counter
  http_request_duration_seconds_bucket{le,...}  histogram
  http_requests_in_flight                       gauge (optional)
  process_cpu_seconds_total / process_resident_memory_bytes
"""
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tinyprom import Counter, Gauge, Histogram, Registry

REG = Registry()
REQS = Counter(REG, "http_requests_total", "HTTP requests")
LAT = Histogram(REG, "http_request_duration_seconds", "HTTP request latency")
INFL = Gauge(REG, "http_requests_in_flight", "Requests currently being served")
ENQ = Counter(REG, "app_jobs_enqueued_total", "Jobs pushed to Redis")

START = time.time()
RUNTIME = {  # live-tunable knobs: curl -XPOST :8000/admin/config -d '{"cpu_ms":40}'
    "cpu_ms": float(os.environ.get("CPU_MS", 10)),
    "sleep_ms": float(os.environ.get("SLEEP_MS", 0)),
    "error_rate": float(os.environ.get("ERROR_RATE", 0.0)),
    "startup_delay_s": float(os.environ.get("STARTUP_DELAY_S", 5)),
    "job_cpu_ms": float(os.environ.get("JOB_CPU_MS", 50)),
    "ballast_mb": 0,
}
_ballast = []
_redis = None


def burn(ms):
    """Burn `ms` of *thread CPU time* (so CFS throttling shows up as latency)."""
    end = time.thread_time() + ms / 1000.0
    x = 0
    while time.thread_time() < end:
        x = (x * 31 + 7) % 1000003
    return x


def redis_client():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                             port=int(os.environ.get("REDIS_PORT", 6379)),
                             socket_timeout=0.5)
    return _redis


def ready():
    return time.time() - START >= RUNTIME["startup_delay_s"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        u = urlparse(self.path)
        path = u.path
        if path == "/metrics":
            return self._send(200, REG.render(), "text/plain; version=0.0.4")
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/readyz":
            return self._send(200 if ready() else 503, "ready" if ready() else "starting", "text/plain")
        if path == "/admin/config":
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                RUNTIME.update(json.loads(self.rfile.read(n) or b"{}"))
                mb = int(RUNTIME.get("ballast_mb", 0))
                _ballast.clear()
                if mb > 0:
                    _ballast.append(bytearray(mb * 1024 * 1024))
            return self._send(200, json.dumps(RUNTIME))

        t0 = time.perf_counter()
        INFL.inc()
        status = 200
        try:
            if path == "/work":
                q = parse_qs(u.query)
                cpu = float(q.get("cpu_ms", [RUNTIME["cpu_ms"]])[0])
                slp = float(q.get("sleep_ms", [RUNTIME["sleep_ms"]])[0])
                burn(cpu)
                if slp:
                    time.sleep(slp / 1000.0)
                if random.random() < RUNTIME["error_rate"]:
                    status = 500
                self._send(status, json.dumps({"ok": status == 200}))
            elif path == "/enqueue":
                try:
                    redis_client().lpush("jobs", json.dumps({"cpu_ms": RUNTIME["job_cpu_ms"],
                                                             "ts": time.time()}))
                    ENQ.inc()
                    self._send(202, '{"queued":true}')
                except Exception as e:
                    status = 503
                    self._send(status, json.dumps({"error": str(e)}))
            else:
                status = 404
                self._send(404, '{"error":"not found"}')
        finally:
            INFL.dec()
            REQS.inc(method=method, path=path if status != 404 else "other", status=str(status))
            LAT.observe(time.perf_counter() - t0, method=method,
                        path=path if status != 404 else "other")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"demo-app listening on :{port}", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()

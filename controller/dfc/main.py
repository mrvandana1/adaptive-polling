"""Controller entrypoint: poll loop + control API + /metrics."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import ConfigStore
from .engine import Engine
from .kube import KubeBackend
from .metrics import REG, Metrics, process_metrics_text
from .policy import Sentinel
from .sources import PodScraper


class EventLog:
    def __init__(self, path, max_events):
        self.buf = deque(maxlen=max_events)
        self.lock = threading.Lock()
        self.f = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.f = open(path, "a", buffering=1)
        except Exception:
            pass

    def __call__(self, rec):
        with self.lock:
            self.buf.append(rec)
            if self.f:
                self.f.write(json.dumps(rec) + "\n")

    def since(self, ts):
        with self.lock:
            return [r for r in self.buf if r.get("ts", 0) >= ts]


def redis_queue_reader():
    cache = {}

    def read(qcfg):
        try:
            import redis
            key = (qcfg["redis_host"], qcfg["redis_port"])
            if key not in cache:
                cache[key] = redis.Redis(host=key[0], port=key[1], socket_timeout=0.5)
            return float(cache[key].llen(qcfg["key"]))
        except Exception:
            return None
    return read


def make_handler(store, engine, log, wake, state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/metrics":
                self._send(200, REG.render() + process_metrics_text(), "text/plain; version=0.0.4")
            elif u.path == "/healthz":
                self._send(200, "ok", "text/plain")
            elif u.path == "/config":
                self._send(200, json.dumps(store.get()))
            elif u.path == "/events":
                since = float(parse_qs(u.query).get("since", ["0"])[0])
                self._send(200, "\n".join(json.dumps(r) for r in log.since(since)),
                           "application/x-ndjson")
            elif u.path == "/state":
                self._send(200, json.dumps(state))
            else:
                self._send(404, '{"error":"not found"}')

        def do_POST(self):
            u = urlparse(self.path)
            body = self._json_body()
            if u.path == "/config":          # deep-merge overlay, live
                store.patch(body)
                wake.set()
                self._send(200, json.dumps(store.get()))
            elif u.path == "/config/reset":
                store.reset_overlay()
                self._send(200, "{}")
            elif u.path == "/mark":          # ground-truth markers from the load generator
                engine.mark(body.get("name", "mark"), time.time())
                self._send(200, "{}")
            elif u.path == "/reset":         # clear history between experiment runs
                engine.reset()
                state["scraper"].reset()
                wake.set()
                self._send(200, "{}")
            else:
                self._send(404, '{"error":"not found"}')
    return H


def main():
    store = ConfigStore(os.environ.get("DFC_CONFIG", "/etc/dfc/config.yaml"))
    cfg = store.get()
    log = EventLog(cfg["log"]["path"], cfg["log"]["max_events"])
    kube = KubeBackend()
    metrics = Metrics()
    engine = Engine(store.get, kube, sink=log, metrics=metrics)
    scraper = PodScraper(kube, store.get, redis_queue_reader())
    wake = threading.Event()
    state = {"scraper": scraper}

    srv = ThreadingHTTPServer(("0.0.0.0", 8080), make_handler(store, engine, log, wake, state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mserv = ThreadingHTTPServer(("0.0.0.0", 9100), make_handler(store, engine, log, wake, state))
    threading.Thread(target=mserv.serve_forever, daemon=True).start()
    print("dfc-controller up: api :8080  metrics :9100", flush=True)

    sentinel = Sentinel()
    while True:
        started = time.monotonic()
        try:
            sample = scraper.poll()
            interval = engine.step(sample)
            sentinel.reset()
        except Exception as e:  # keep the loop alive in a lab setting
            print("poll error:", repr(e), flush=True)
            interval = 5.0
        # Sleep until the next full poll; in dynamic mode run cheap sentinel probes meanwhile.
        deadline = started + interval
        while True:
            remaining = deadline - time.monotonic()
            p = store.get()["polling"]
            tick = p["sentinel_interval_s"] if (p["mode"] == "dynamic" and p["sentinel_interval_s"] > 0) else 0
            if remaining <= 0:
                break
            if wake.wait(timeout=min(tick, remaining) if tick else remaining):
                break
            if tick:
                pr = scraper.probe()
                if pr:
                    metrics.scrapes.inc(1)
                    metrics.scrape_bytes.inc(pr["bytes"])
                    metrics.probes.inc()
                    sentinel.update(pr["t"], pr["pod"], pr["cum"], pr["n"])
                    est = sentinel.estimate(pr["t"], p["sentinel_window_s"])
                    if Sentinel.triggered(est, engine.last_rate, p):
                        engine.sink({"type": "sentinel_trigger", "ts": pr["t"], "est": est,
                                     "last_rate": engine.last_rate})
                        break
        wake.clear()


if __name__ == "__main__":
    main()

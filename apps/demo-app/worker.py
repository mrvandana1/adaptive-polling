"""Redis queue consumer (same image as the app; run with `python worker.py`).

Exposes worker_* metrics on :8001 so queue depth / job latency are collected too.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis

from app import burn
from tinyprom import Counter, Gauge, Histogram, Registry

REG = Registry()
DONE = Counter(REG, "worker_jobs_processed_total", "Jobs processed")
DUR = Histogram(REG, "worker_job_duration_seconds", "Job processing time")
WAIT = Histogram(REG, "worker_job_queue_wait_seconds", "Time a job waited in the queue",
                 buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
QD = Gauge(REG, "worker_queue_depth", "Queue depth seen by worker")

r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                port=int(os.environ.get("REDIS_PORT", 6379)), socket_timeout=5)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = REG.render().encode() if self.path == "/metrics" else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve():
    ThreadingHTTPServer(("0.0.0.0", 8001), H).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    print("worker started", flush=True)
    while True:
        try:
            item = r.brpop("jobs", timeout=2)
            QD.set(r.llen("jobs"))
            if not item:
                continue
            job = json.loads(item[1])
            t0 = time.perf_counter()
            WAIT.observe(max(time.time() - job.get("ts", time.time()), 0))
            burn(float(job.get("cpu_ms", 50)))
            DUR.observe(time.perf_counter() - t0)
            DONE.inc()
        except Exception as e:
            print("worker error:", e, flush=True)
            time.sleep(1)

"""Controller's own Prometheus metrics (dfc_*). Overhead metrics included."""
from __future__ import annotations

import os
import time

from .promtext import Registry

REG = Registry()
_START = time.time()


class Metrics:
    def __init__(self, reg=REG):
        self.reg = reg
        self.polls = reg.counter("dfc_polls_total", "Polling cycles executed")
        self.probes = reg.counter("dfc_sentinel_probes_total", "Cheap sentinel probes (1 pod)")
        self.scrapes = reg.counter("dfc_scrapes_total", "Pod /metrics scrape requests issued")
        self.scrape_bytes = reg.counter("dfc_scrape_bytes_total", "Bytes fetched from pods")
        self.scale_actions = reg.counter("dfc_scale_actions_total", "Scale actions by direction")
        self.poll_seconds = reg.gauge("dfc_poll_duration_seconds", "Duration of last poll")
        self.interval = reg.gauge("dfc_poll_interval_seconds", "Chosen next polling interval")
        self.rate = reg.gauge("dfc_observed_rate", "Observed request rate (rps)")
        self.forecast = reg.gauge("dfc_forecast_rate", "Forecast of scaling metric at horizon")
        self.velocity = reg.gauge("dfc_velocity", "dQ/dt (rps/s)")
        self.accel = reg.gauge("dfc_acceleration", "d2Q/dt2 (rps/s^2)")
        self.score = reg.gauge("dfc_change_score", "Normalised change score driving interval")
        self.cpu = reg.gauge("dfc_cpu_cores", "App CPU cores used (from app counters)")
        self.p95 = reg.gauge("dfc_p95_latency_seconds", "p95 latency over the rate window")
        self.queue = reg.gauge("dfc_queue_depth", "Redis queue depth")
        self.desired = reg.gauge("dfc_desired_replicas", "Replicas decided by controller")
        self.spec = reg.gauge("dfc_current_replicas", "Deployment spec.replicas")
        self.ready = reg.gauge("dfc_ready_replicas", "Deployment ready replicas")
        self.detect = reg.gauge("dfc_last_detection_latency_seconds",
                                "Burst-mark to detection latency")


def process_metrics_text() -> str:
    """Process CPU/RSS of the controller itself (monitoring-overhead evidence)."""
    cpu = time.process_time()
    rss = 0
    try:
        with open("/proc/self/statm") as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    return (f"# TYPE process_cpu_seconds_total counter\nprocess_cpu_seconds_total {cpu}\n"
            f"# TYPE process_resident_memory_bytes gauge\nprocess_resident_memory_bytes {rss}\n")

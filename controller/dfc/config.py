"""Hot-reloadable configuration: YAML file (ConfigMap) + in-memory overlay (API)."""
from __future__ import annotations

import copy
import os
import threading

DEFAULTS = {
    "target": {
        "namespace": "autoscale-lab",
        "deployment": "demo-app",
        "selector": "app=demo-app",
        "metrics_port": 8000,
        "metrics_path": "/metrics",
        "scrape_timeout_s": 0.8,
    },
    # App-side metric names. Change these (live) when you swap the application.
    "metrics": {
        "request_counter": "http_requests_total",
        "error_label": "status",          # label whose value starting with "5" = error
        "latency_histogram": "http_request_duration_seconds",
        "cpu_counter": "process_cpu_seconds_total",
        "mem_gauge": "process_resident_memory_bytes",
        "inflight_gauge": "http_requests_in_flight",
    },
    "queue": {
        "enabled": True,
        "redis_host": "redis",
        "redis_port": 6379,
        "key": "jobs",
    },
    "scaling": {
        "enabled": True,
        "metric": "rps",            # rps | cpu | queue
        "target_per_pod": 30.0,     # rps/pod, cores/pod, or backlog/pod
        "min_replicas": 1,
        "max_replicas": 20,
        "tolerance": 0.1,
        "scale_down_stabilization_s": 60,
        "scale_up_max_step_min": 4,     # HPA-like: up to max(4, 2x) per decision
        "scale_up_max_factor": 2.0,
    },
    "polling": {
        "mode": "dynamic",          # fixed | dynamic
        "fixed_interval_s": 15.0,   # HPA sync period default
        "min_interval_s": 1.0,
        "max_interval_s": 30.0,     # KEDA pollingInterval default
        "rate_window_s": 5.0,       # min trailing window for rate estimates
        "velocity_ref": 0.15,       # relative change /s that counts as "fast"
        "accel_ref": 0.05,          # relative change /s^2 that counts as "fast"
        "rate_floor": 5.0,          # rps floor used to normalise (noise guard)
        "steepness": 2.0,           # non-linearity exponent of interval curve
        "release": 0.3,             # fraction of the gap closed per poll when relaxing
        "smoothing": 0.5,           # EWMA for velocity/accel
        # Sentinel: cheap 1-pod probe between full polls (dynamic mode only; 0 = off).
        "sentinel_interval_s": 2.0,
        "sentinel_window_s": 4.0,
        "sentinel_ratio": 1.5,
        "sentinel_abs": 8.0,
    },
    "forecast": {
        "enabled": False,
        "horizon_s": 30.0,          # ~ pod startup + one poll
        "alpha": 0.6,               # Holt level smoothing
        "beta": 0.4,                # Holt trend smoothing
        "max_ratio": 3.0,           # forecast capped at ratio x observed
    },
    "tracking": {
        "detect_ratio": 1.5,
        "detect_abs": 5.0,
    },
    "slo": {"p95_s": 0.25, "error_ratio": 0.01},
    "log": {"path": "/data/events.jsonl", "max_events": 50000},
}


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ConfigStore:
    def __init__(self, path: str | None = None):
        self.path = path
        self._mtime = None
        self._file_cfg: dict = {}
        self._overlay: dict = {}
        self._lock = threading.Lock()

    def _maybe_reload(self):
        if not self.path or not os.path.exists(self.path):
            return
        m = os.path.getmtime(self.path)
        if m != self._mtime:
            import yaml
            with open(self.path) as f:
                self._file_cfg = yaml.safe_load(f) or {}
            self._mtime = m

    def get(self) -> dict:
        with self._lock:
            self._maybe_reload()
            return deep_merge(deep_merge(DEFAULTS, self._file_cfg), self._overlay)

    def patch(self, patch: dict):
        with self._lock:
            self._overlay = deep_merge(self._overlay, patch)

    def reset_overlay(self):
        with self._lock:
            self._overlay = {}

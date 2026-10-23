"""Decision engine: one step() per poll. Pure w.r.t. I/O (actuator + sink injected)."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from .policy import AdaptivePoller, Holt, ScalerPolicy, histogram_quantile


@dataclass
class Sample:
    t: float
    cum_req: float = 0.0
    cum_err: float = 0.0
    cum_cpu: float = 0.0
    buckets: dict = field(default_factory=dict)   # le -> cumulative count
    mem_bytes: float = 0.0
    inflight: float = 0.0
    queue_depth: float | None = None
    spec_replicas: int = 1
    ready_replicas: int = 1
    pods_scraped: int = 0
    scrapes: int = 0          # scrape requests made in THIS poll
    scrape_bytes: int = 0     # bytes fetched in THIS poll
    poll_seconds: float = 0.0


class Engine:
    def __init__(self, cfg_getter, actuator, sink=None, metrics=None):
        self.cfg_getter = cfg_getter
        self.actuator = actuator          # object with set_replicas(n)
        self.sink = sink or (lambda rec: None)
        self.m = metrics
        self.lock = threading.RLock()
        self.reset()

    # ---- lifecycle -----------------------------------------------------
    def reset(self):
        with self.lock:
            self.hist = deque()
            self.poller = AdaptivePoller()
            self.scaler = ScalerPolicy()
            self.holt = Holt()
            self.last_rate = None
            self.mark_state = None   # active burst mark awaiting detection
            self.last_detection_latency = None

    def mark(self, name: str, now: float):
        with self.lock:
            self.sink({"type": "mark", "name": name, "ts": now})
            if name == "burst_start":
                self.mark_state = {
                    "t": now, "baseline": self.last_rate or 0.0,
                    "detected": False, "scaled": False,
                    "spec_at_mark": self.hist[-1].spec_replicas if self.hist else None,
                }

    # ---- helpers ---------------------------------------------------------
    def _ref(self, now, window):
        ref = self.hist[0]
        for s in reversed(self.hist):
            if s is not self.hist[-1] and s.t <= now - window:
                ref = s
                break
        return ref

    # ---- main step -----------------------------------------------------
    def step(self, s: Sample) -> float:
        with self.lock:
            cfg = self.cfg_getter()
            pol, scl, fc, trk = cfg["polling"], cfg["scaling"], cfg["forecast"], cfg["tracking"]
            self.hist.append(s)
            while self.hist and self.hist[0].t < s.t - 180:
                self.hist.popleft()

            rec = {"type": "poll", "ts": s.t, "mode": pol["mode"],
                   "forecast_on": fc["enabled"], "spec": s.spec_replicas,
                   "ready": s.ready_replicas, "pods": s.pods_scraped,
                   "scrapes": s.scrapes, "scrape_bytes": s.scrape_bytes,
                   "poll_s": s.poll_seconds, "mem": s.mem_bytes,
                   "inflight": s.inflight, "queue": s.queue_depth}

            if len(self.hist) < 2:
                interval = pol["min_interval_s"] if pol["mode"] == "dynamic" else pol["fixed_interval_s"]
                rec.update(interval=interval, rate=None)
                self._emit(rec, s)
                return interval

            window = max(pol["rate_window_s"], 0.0)
            ref = self._ref(s.t, window)
            dt = max(s.t - ref.t, 1e-6)
            rate = max(s.cum_req - ref.cum_req, 0.0) / dt
            err_rate = max(s.cum_err - ref.cum_err, 0.0) / dt
            cpu = max(s.cum_cpu - ref.cum_cpu, 0.0) / dt
            bdelta = {le: max(c - ref.buckets.get(le, 0.0), 0.0) for le, c in s.buckets.items()}
            p95 = histogram_quantile(0.95, bdelta)
            p50 = histogram_quantile(0.50, bdelta)

            v, a = self.poller.observe(s.t, rate, pol)

            value = {"rps": rate, "cpu": cpu,
                     "queue": s.queue_depth if s.queue_depth is not None else 0.0}[scl["metric"]]
            self.holt.update(s.t, value)
            forecast = self.holt.forecast(fc["horizon_s"])
            if forecast is not None:
                forecast = min(forecast, max(value, 1e-9) * fc["max_ratio"]) if value > 0 else forecast
            effective = value
            if fc["enabled"] and forecast is not None:
                effective = max(value, forecast)

            desired = s.spec_replicas
            if scl["enabled"]:
                desired = self.scaler.desired(s.t, s.spec_replicas, effective, scl)
                if desired != s.spec_replicas:
                    self.actuator.set_replicas(desired)
                    self.sink({"type": "scale", "ts": s.t, "from": s.spec_replicas,
                               "to": desired, "value": value, "forecast": forecast})
                    if self.m:
                        self.m.scale_actions.inc(direction="up" if desired > s.spec_replicas else "down")

            interval = self.poller.next_interval(rate, pol)
            self._track(s, rate, desired, trk)
            self.last_rate = rate

            rec.update(interval=interval, rate=rate, err_rate=err_rate, cpu=cpu,
                       p50=p50, p95=p95, velocity=v, accel=a,
                       score=self.poller.score, value=value, forecast=forecast,
                       forecast_h=fc["horizon_s"], desired=desired)
            self._emit(rec, s)
            return interval

    def _track(self, s, rate, desired, trk):
        ms = self.mark_state
        if not ms:
            return
        if not ms["detected"] and rate >= max(ms["baseline"] * trk["detect_ratio"],
                                              ms["baseline"] + trk["detect_abs"]):
            ms["detected"] = True
            lat = s.t - ms["t"]
            self.last_detection_latency = lat
            self.sink({"type": "detect", "ts": s.t, "latency_s": lat,
                       "rate": rate, "baseline": ms["baseline"]})
        if not ms["scaled"] and ms["spec_at_mark"] is not None and desired > ms["spec_at_mark"]:
            ms["scaled"] = True
            self.sink({"type": "first_scale", "ts": s.t, "latency_s": s.t - ms["t"],
                       "to": desired})

    def _emit(self, rec, s):
        self.sink(rec)
        m = self.m
        if not m:
            return
        m.polls.inc()
        m.scrapes.inc(s.scrapes)
        m.scrape_bytes.inc(s.scrape_bytes)
        m.poll_seconds.set(s.poll_seconds)
        m.interval.set(rec.get("interval", 0))
        m.spec.set(s.spec_replicas)
        m.ready.set(s.ready_replicas)
        for key, g in (("rate", m.rate), ("forecast", m.forecast), ("velocity", m.velocity),
                       ("accel", m.accel), ("cpu", m.cpu), ("p95", m.p95), ("desired", m.desired),
                       ("score", m.score)):
            if rec.get(key) is not None:
                g.set(rec[key])
        if s.queue_depth is not None:
            m.queue.set(s.queue_depth)
        if self.last_detection_latency is not None:
            m.detect.set(self.last_detection_latency)

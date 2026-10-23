"""Pure policy math: windowed rates, quantiles, Holt forecast, adaptive interval, scaling.

No I/O here so the same code runs in the real controller and in simulate.py.
"""
from __future__ import annotations

import math


def histogram_quantile(q: float, buckets: dict) -> float | None:
    """buckets: {le(float): cumulative_count_delta}. Linear interpolation like PromQL."""
    if not buckets:
        return None
    items = sorted(buckets.items())
    total = items[-1][1]
    if total <= 0:
        return None
    rank = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in items:
        if c >= rank:
            if math.isinf(le):
                return prev_le
            if c == prev_c:
                return le
            return prev_le + (le - prev_le) * (rank - prev_c) / (c - prev_c)
        prev_le, prev_c = le, c
    return prev_le


class Holt:
    """Holt linear (double exponential) smoothing for irregular sampling."""

    def __init__(self, alpha=0.6, beta=0.4):
        self.alpha, self.beta = alpha, beta
        self.level = None
        self.trend = 0.0
        self.t = None

    def update(self, t: float, x: float):
        if self.level is None:
            self.level, self.t = x, t
            return
        dt = max(t - self.t, 1e-6)
        pred = self.level + self.trend * dt
        new_level = self.alpha * x + (1 - self.alpha) * pred
        inst_trend = (new_level - self.level) / dt
        self.trend = self.beta * inst_trend + (1 - self.beta) * self.trend
        self.level, self.t = new_level, t

    def forecast(self, h: float) -> float | None:
        if self.level is None:
            return None
        return max(0.0, self.level + self.trend * h)


class AdaptivePoller:
    """Maps rate velocity / acceleration to the next polling interval.

    interval = min + (max - min) / (1 + score^steepness),
    score = max(|v|/v_ref, |a|/a_ref) with v, a normalised by max(rate, floor).
    Speeds up immediately, relaxes gradually ('release').
    """

    def __init__(self):
        self.rate_prev = None
        self.t_prev = None
        self.v_s = 0.0
        self.a_s = 0.0
        self.interval = None
        self.score = 0.0

    def reset(self):
        self.__init__()

    def observe(self, t: float, rate: float, p: dict):
        """Update velocity/accel (rate units: rps, rps/s, rps/s^2)."""
        if self.rate_prev is not None:
            dt = max(t - self.t_prev, 1e-6)
            v = (rate - self.rate_prev) / dt
            a_raw = (v - self.v_s) / dt
            s = p["smoothing"]
            self.a_s = s * a_raw + (1 - s) * self.a_s
            self.v_s = s * v + (1 - s) * self.v_s
        self.rate_prev, self.t_prev = rate, t
        return self.v_s, self.a_s

    def next_interval(self, rate: float, p: dict) -> float:
        if p["mode"] == "fixed":
            self.interval = p["fixed_interval_s"]
            return self.interval
        base = max(rate, p["rate_floor"])
        score = max(abs(self.v_s) / (p["velocity_ref"] * base),
                    abs(self.a_s) / (p["accel_ref"] * base))
        self.score = score
        lo, hi = p["min_interval_s"], p["max_interval_s"]
        target = lo + (hi - lo) / (1.0 + score ** p["steepness"])
        if self.interval is not None and target > self.interval:
            target = self.interval + p["release"] * (target - self.interval)
        self.interval = min(max(target, lo), hi)
        return self.interval


class Sentinel:
    """Cheap between-poll probe: watches ONE pod's request counter and extrapolates.

    Costs 1 scrape per tick instead of N scrapes + API calls, and wakes the full poll
    early when the estimated rate jumps. This is what lets a long steady-state interval
    (up to 30 s) coexist with fast burst detection.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.h = []          # (t, pod_cum)
        self.pod = None
        self.n_pods = 1

    def update(self, t, pod, cum, n_pods):
        if pod != self.pod:
            self.h, self.pod = [], pod
        self.n_pods = max(n_pods, 1)
        self.h.append((t, cum))
        self.h = self.h[-64:]

    def estimate(self, t, window):
        if len(self.h) < 2:
            return None
        ref = self.h[0]
        for s in reversed(self.h[:-1]):
            if s[0] <= t - window:
                ref = s
                break
        dt = t - ref[0]
        if dt <= 0:
            return None
        return max(self.h[-1][1] - ref[1], 0.0) / dt * self.n_pods

    @staticmethod
    def triggered(est, last_rate, p) -> bool:
        if est is None or last_rate is None:
            return False
        return est >= max(last_rate * p["sentinel_ratio"], last_rate + p["sentinel_abs"])


class ScalerPolicy:
    """HPA-like replica calculation with optional forecast input."""

    def __init__(self):
        self.history = []  # (t, desired)

    def reset(self):
        self.history = []

    def desired(self, t: float, current: int, value: float, p: dict) -> int:
        target = p["target_per_pod"]
        cur = max(current, 1)
        ratio = value / (cur * target)
        if abs(ratio - 1.0) <= p["tolerance"]:
            raw = cur
        else:
            raw = math.ceil(value / target)
        raw = min(max(raw, p["min_replicas"]), p["max_replicas"])
        if raw > cur:
            limit = max(p["scale_up_max_step_min"], math.ceil(cur * p["scale_up_max_factor"]))
            raw = min(raw, cur + limit)
        self.history.append((t, raw))
        win = p["scale_down_stabilization_s"]
        self.history = [(ts, d) for ts, d in self.history if ts >= t - win]
        stabilised = max(d for _, d in self.history)
        # stabilisation only delays scale-down; never blocks scale-up
        return max(raw, stabilised) if raw < cur else raw

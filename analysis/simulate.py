#!/usr/bin/env python3
"""Offline simulation of configs A-D with the SAME engine code as the real controller.

Virtual time, Poisson-noisy request counters, pod startup delay, per-pod capacity.
Use it to sanity-check policy parameters before burning cluster time.

  python3 analysis/simulate.py --scenario accel --seeds 10
"""
import argparse
import math
import os
import random
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "controller"))
sys.path.insert(0, HERE)

from dfc.config import DEFAULTS, deep_merge  # noqa: E402
from dfc.engine import Engine, Sample  # noqa: E402
from dfc.policy import Sentinel  # noqa: E402
import profiles  # noqa: E402

CONFIGS = {
    "A": {"polling": {"mode": "fixed"}, "forecast": {"enabled": False}},
    "B": {"polling": {"mode": "fixed"}, "forecast": {"enabled": True}},
    "C": {"polling": {"mode": "dynamic"}, "forecast": {"enabled": False}},
    "D": {"polling": {"mode": "dynamic"}, "forecast": {"enabled": True}},
}


class SimEnv:
    def __init__(self, pts, rng, startup_s=15.0, cap_per_pod=50.0, cpu_ms=10.0):
        self.pts, self.rng = pts, rng
        self.startup, self.cap, self.cpu_ms = startup_s, cap_per_pod, cpu_ms
        self.pods = [-1e9]          # ready_at times
        self.cum_req = 0.0
        self.probe_cum = 0.0
        self.cum_cpu = 0.0
        self.t = 0.0
        self.sat_seconds = 0.0
        self.timeline = []          # (t, ready, spec)

    def ready(self, t=None):
        t = self.t if t is None else t
        return sum(1 for r in self.pods if r <= t)

    def set_replicas(self, n):
        if n > len(self.pods):
            self.pods += [self.t + self.startup] * (n - len(self.pods))
        elif n < len(self.pods):
            self.pods = sorted(self.pods)[:n]

    def advance(self, dt):
        rate = profiles.rate_at(self.pts, self.t)
        cap = self.ready() * self.cap
        served = min(rate, cap)
        lam = served * dt
        n = max(0.0, lam + self.rng.gauss(0, math.sqrt(max(lam, 1e-9))))
        self.cum_req += n
        lp = lam / max(self.ready(), 1)
        self.probe_cum += max(0.0, lp + self.rng.gauss(0, math.sqrt(max(lp, 1e-9))))
        self.cum_cpu += n * self.cpu_ms / 1000.0
        if rate > cap:
            self.sat_seconds += dt
        self.t += dt
        self.timeline.append((self.t, self.ready(), len(self.pods)))

    def sample(self):
        return Sample(t=self.t, cum_req=self.cum_req, cum_cpu=self.cum_cpu,
                      spec_replicas=len(self.pods), ready_replicas=self.ready(),
                      pods_scraped=self.ready(), scrapes=self.ready(),
                      scrape_bytes=self.ready() * 2500)


def run_one(cfg_name, scenario, seed, overrides=None, warm_jitter=15.0, events_out=None, cfg_out=None, **kw):
    rng = random.Random(seed)
    warm = 60.0 + rng.uniform(0, warm_jitter)       # randomise burst/poll phase
    pts, marks = profiles.points(scenario, warm=warm, **kw)
    cfg = deep_merge(deep_merge(DEFAULTS, CONFIGS[cfg_name]), overrides or {})
    cfg["scaling"]["scale_down_stabilization_s"] = 60
    env = SimEnv(pts, rng)
    events = events_out if events_out is not None else []
    if cfg_out is not None:
        cfg_out.update(cfg)
    eng = Engine(lambda: cfg, env, sink=events.append)
    next_poll, next_probe, marked = 0.0, 0.0, False
    sent, probes = Sentinel(), 0
    dur = profiles.duration(pts)
    polls = 0
    while env.t < dur:
        if not marked and "burst_start" in marks and env.t >= marks["burst_start"]:
            eng.mark("burst_start", env.t)
            marked = True
        if env.t >= next_poll:
            interval = eng.step(env.sample())
            next_poll = env.t + interval
            polls += 1
            sent.reset()
        pol = cfg["polling"]
        if pol["mode"] == "dynamic" and pol["sentinel_interval_s"] > 0 and env.t >= next_probe:
            next_probe = env.t + pol["sentinel_interval_s"]
            probes += 1
            sent.update(env.t, "p0", env.probe_cum, env.ready())
            est = sent.estimate(env.t, pol["sentinel_window_s"])
            if Sentinel.triggered(est, eng.last_rate, pol):
                next_poll = env.t
        env.advance(0.25)

    out = {"polls": polls, "probes": probes, "sat_s": env.sat_seconds, "peak_pods": max(s for _, _, s in env.timeline)}
    if marks:
        thr = cfg["tracking"]["detect_ratio"] * pts[0][1]
        t_cross = profiles.crossing_time(pts, max(thr, pts[0][1] + cfg["tracking"]["detect_abs"]))
        det = next((e for e in events if e["type"] == "detect"), None)
        fs = next((e for e in events if e["type"] == "first_scale"), None)
        out["detect_delay"] = (det["ts"] - t_cross) if det and t_cross is not None else None
        out["first_scale_delay"] = (fs["ts"] - t_cross) if fs and t_cross is not None else None
        need = math.ceil(max(r for _, r in pts) / cfg["scaling"]["target_per_pod"]) if scenario != "steady" else 1
        cap_t = next((t for t, rdy, _ in env.timeline if t >= marks["burst_start"] and rdy >= need), None)
        out["time_to_capacity"] = (cap_t - marks["burst_start"]) if cap_t else None
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="accel", choices=["step", "ramp", "accel", "steady"])
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--max-interval", type=float, default=None)
    ap.add_argument("--sentinel", type=float, default=None, help="sentinel interval s (0=off)")
    ap.add_argument("--dump", metavar="DIR", help="write run dirs analysable by analyze.py")
    args = ap.parse_args()
    ov = {"polling": {}}
    if args.max_interval:
        ov["polling"]["max_interval_s"] = args.max_interval
    if args.sentinel is not None:
        ov["polling"]["sentinel_interval_s"] = args.sentinel
    if args.dump:
        import json
        for c in "ABCD":
            ev, cf = [], {}
            run_one(c, args.scenario, 0, ov, events_out=ev, cfg_out=cf)
            d = os.path.join(args.dump, f"sim_{c}_{args.scenario}_r1")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "events.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(e) for e in ev))
            json.dump(cf, open(os.path.join(d, "config.json"), "w"))
            json.dump({"run": os.path.basename(d), "config": c, "scenario": args.scenario, "rep": 1,
                       "start": 0, "end": 400, "params": {"base": 10.0, "peak": 150.0}},
                      open(os.path.join(d, "meta.json"), "w"))
        print("dumped to", args.dump)
        return
    print(f"scenario={args.scenario} seeds={args.seeds} max_interval={args.max_interval or 30} "
          f"sentinel={args.sentinel if args.sentinel is not None else 2.0}s")
    print(f"{'cfg':<4}{'detect_delay_s':>16}{'first_scale_s':>15}{'to_capacity_s':>15}"
          f"{'saturated_s':>13}{'polls':>7}{'probes':>8}")
    for c in "ABCD":
        rs = [run_one(c, args.scenario, s, ov) for s in range(args.seeds)]
        print(f"{c:<4}{mean([r.get('detect_delay') for r in rs]):>16.1f}"
              f"{mean([r.get('first_scale_delay') for r in rs]):>15.1f}"
              f"{mean([r.get('time_to_capacity') for r in rs]):>15.1f}"
              f"{mean([r['sat_s'] for r in rs]):>13.1f}{mean([r['polls'] for r in rs]):>7.0f}"
              f"{mean([r['probes'] for r in rs]):>8.0f}")


if __name__ == "__main__":
    main()

"""Turns raw pod /metrics scrapes into monotone cumulative counters (survives pod churn)."""
from __future__ import annotations

import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import promtext
from .engine import Sample


def parse_pod_metrics(text: str, mcfg: dict) -> dict:
    out = {"req": 0.0, "err": 0.0, "cpu": 0.0, "mem": 0.0, "inflight": 0.0,
           "buckets": defaultdict(float)}
    hist_bucket = mcfg["latency_histogram"] + "_bucket"
    for name, labels, value in promtext.parse(text):
        if name == mcfg["request_counter"]:
            out["req"] += value
            if str(labels.get(mcfg["error_label"], "")).startswith("5"):
                out["err"] += value
        elif name == hist_bucket:
            le = labels.get("le", "+Inf")
            out["buckets"][float("inf") if le in ("+Inf", "Inf") else float(le)] += value
        elif name == mcfg["cpu_counter"]:
            out["cpu"] += value
        elif name == mcfg["mem_gauge"]:
            out["mem"] += value
        elif name == mcfg["inflight_gauge"]:
            out["inflight"] += value
    return out


class Accumulator:
    """Sums per-pod counter deltas into cluster-wide cumulative counters.

    A pod seen for the first time *after* the controller is initialised contributes
    its whole counter (its counters started at 0 when it started). Counter resets
    (pod restarts under the same name) are handled by treating a drop as a reset.
    """

    def __init__(self):
        self.cum = defaultdict(float)
        self.last = {}
        self.initialised = False

    def add(self, pod, key, value):
        k = (pod, key)
        if k in self.last:
            d = value - self.last[k]
            if d < 0:
                d = value
        else:
            d = value if self.initialised else 0.0
        self.last[k] = value
        self.cum[key] += d

    def finish_poll(self, live_pods):
        self.initialised = True
        for k in [k for k in self.last if k[0] not in live_pods]:
            del self.last[k]


class PodScraper:
    def __init__(self, kube, cfg_getter, queue_reader=None):
        self.kube = kube
        self.cfg_getter = cfg_getter
        self.queue_reader = queue_reader
        self.acc = Accumulator()
        self.pool = ThreadPoolExecutor(max_workers=16)

    def reset(self):
        self.acc = Accumulator()
        self.pods = []

    def probe(self):
        """Sentinel probe: scrape one pod (stable choice) using the cached pod list."""
        cfg = self.cfg_getter()
        pods = sorted(getattr(self, "pods", []))
        if not pods:
            return None
        name, ip = pods[0]
        text, size = self._scrape(ip, cfg["target"])
        if text is None:
            return None
        m = parse_pod_metrics(text, cfg["metrics"])
        return {"t": time.time(), "pod": name, "cum": m["req"], "n": len(pods), "bytes": size}

    def _scrape(self, ip, tcfg):
        url = f"http://{ip}:{tcfg['metrics_port']}{tcfg['metrics_path']}"
        try:
            with urllib.request.urlopen(url, timeout=tcfg["scrape_timeout_s"]) as r:
                body = r.read()
            return body.decode("utf-8", "replace"), len(body)
        except Exception:
            return None, 0

    def poll(self) -> Sample:
        cfg = self.cfg_getter()
        tcfg, mcfg = cfg["target"], cfg["metrics"]
        t0 = time.time()
        pods = self.kube.pods(tcfg["namespace"], tcfg["selector"])
        self.pods = pods
        spec, ready = self.kube.replicas(tcfg["namespace"], tcfg["deployment"])
        results = list(self.pool.map(lambda p: (p[0], *self._scrape(p[1], tcfg)), pods))
        mem = infl = 0.0
        scraped, nbytes = 0, 0
        live = set()
        for name, text, size in results:
            nbytes += size
            if text is None:
                continue
            scraped += 1
            live.add(name)
            m = parse_pod_metrics(text, mcfg)
            self.acc.add(name, "req", m["req"])
            self.acc.add(name, "err", m["err"])
            self.acc.add(name, "cpu", m["cpu"])
            for le, c in m["buckets"].items():
                self.acc.add(name, ("b", le), c)
            mem += m["mem"]
            infl += m["inflight"]
        self.acc.finish_poll(live)
        qd = None
        if cfg["queue"]["enabled"] and self.queue_reader:
            qd = self.queue_reader(cfg["queue"])
        buckets = {k[1]: v for k, v in self.acc.cum.items() if isinstance(k, tuple)}
        t1 = time.time()
        return Sample(t=t1, cum_req=self.acc.cum["req"], cum_err=self.acc.cum["err"],
                      cum_cpu=self.acc.cum["cpu"], buckets=buckets, mem_bytes=mem,
                      inflight=infl, queue_depth=qd, spec_replicas=spec,
                      ready_replicas=ready, pods_scraped=scraped,
                      scrapes=len(pods), scrape_bytes=nbytes, poll_seconds=t1 - t0)

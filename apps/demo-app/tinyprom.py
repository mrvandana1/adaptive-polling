"""Tiny stdlib Prometheus exposition helper (counter / gauge / histogram + process metrics)."""
import os
import threading
import time

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _lbl(d):
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}" if d else ""


class Registry:
    def __init__(self):
        self.metrics = []
        self.lock = threading.Lock()

    def add(self, m):
        self.metrics.append(m)
        return m

    def render(self):
        out = []
        for m in self.metrics:
            out.extend(m.render())
        out.extend(process_metrics())
        return "\n".join(out) + "\n"


class Counter:
    def __init__(self, reg, name, help_=""):
        self.name, self.help, self.v, self.lock = name, help_, {}, threading.Lock()
        reg.add(self)

    def inc(self, n=1.0, **labels):
        k = tuple(sorted(labels.items()))
        with self.lock:
            self.v[k] = self.v.get(k, 0.0) + n

    def render(self):
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self.lock:
            for k, v in self.v.items():
                out.append(f"{self.name}{_lbl(dict(k))} {v}")
        return out


class Gauge:
    def __init__(self, reg, name, help_="", fn=None):
        self.name, self.help, self.val, self.fn = name, help_, 0.0, fn
        self.lock = threading.Lock()
        reg.add(self)

    def set(self, v):
        self.val = float(v)

    def inc(self, n=1.0):
        with self.lock:
            self.val += n

    def dec(self, n=1.0):
        self.inc(-n)

    def render(self):
        v = self.fn() if self.fn else self.val
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge", f"{self.name} {v}"]


class Histogram:
    def __init__(self, reg, name, help_="", buckets=DEFAULT_BUCKETS):
        self.name, self.help, self.b = name, help_, tuple(buckets)
        self.lock = threading.Lock()
        self.counts, self.sums, self.totals = {}, {}, {}
        reg.add(self)

    def observe(self, x, **labels):
        k = tuple(sorted(labels.items()))
        with self.lock:
            c = self.counts.setdefault(k, [0] * len(self.b))
            for i, ub in enumerate(self.b):
                if x <= ub:
                    c[i] += 1
            self.sums[k] = self.sums.get(k, 0.0) + x
            self.totals[k] = self.totals.get(k, 0) + 1

    def render(self):
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self.lock:
            for k, c in self.counts.items():
                base = dict(k)
                for ub, n in zip(self.b, c):
                    out.append(f"{self.name}_bucket{_lbl({**base, 'le': ub})} {n}")
                out.append(f"{self.name}_bucket{_lbl({**base, 'le': '+Inf'})} {self.totals[k]}")
                out.append(f"{self.name}_sum{_lbl(base)} {self.sums[k]}")
                out.append(f"{self.name}_count{_lbl(base)} {self.totals[k]}")
        return out


def process_metrics():
    rss = 0
    try:
        with open("/proc/self/statm") as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    return [
        "# TYPE process_cpu_seconds_total counter",
        f"process_cpu_seconds_total {time.process_time()}",
        "# TYPE process_resident_memory_bytes gauge",
        f"process_resident_memory_bytes {rss}",
        "# TYPE process_threads gauge",
        f"process_threads {threading.active_count()}",
    ]

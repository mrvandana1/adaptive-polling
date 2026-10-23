"""Minimal Prometheus text-format parser and renderer (stdlib only)."""
from __future__ import annotations

import re
import threading
from collections import defaultdict

_LINE = re.compile(
    r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([^\s]+)(?:\s+\d+)?$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse(text: str):
    """Yield (name, labels_dict, value) for every sample line."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, raw_labels, val = m.groups()
        try:
            value = float(val)
        except ValueError:
            continue
        labels = dict(_LABEL.findall(raw_labels)) if raw_labels else {}
        yield name, labels, value


def _fmt_labels(labels: dict) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class Registry:
    """Tiny thread-safe registry of gauges and counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self._help = {}
        self._type = {}
        self._vals = defaultdict(dict)  # name -> {labels_tuple: value}

    def _declare(self, name, kind, help_):
        self._help.setdefault(name, help_)
        self._type.setdefault(name, kind)

    def gauge(self, name, help_=""):
        self._declare(name, "gauge", help_)
        return _Child(self, name, "gauge")

    def counter(self, name, help_=""):
        self._declare(name, "counter", help_)
        return _Child(self, name, "counter")

    def render(self) -> str:
        out = []
        with self._lock:
            for name in sorted(self._vals):
                out.append(f"# HELP {name} {self._help.get(name, '')}")
                out.append(f"# TYPE {name} {self._type[name]}")
                for lt, v in sorted(self._vals[name].items()):
                    out.append(f"{name}{_fmt_labels(dict(lt))} {v}")
        return "\n".join(out) + "\n"


class _Child:
    def __init__(self, reg, name, kind):
        self.reg, self.name, self.kind = reg, name, kind

    def set(self, value, **labels):
        with self.reg._lock:
            self.reg._vals[self.name][tuple(sorted(labels.items()))] = float(value)

    def inc(self, amount=1.0, **labels):
        key = tuple(sorted(labels.items()))
        with self.reg._lock:
            d = self.reg._vals[self.name]
            d[key] = d.get(key, 0.0) + amount

import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "controller"))
sys.path.insert(0, os.path.join(ROOT, "apps", "demo-app"))

from dfc import promtext  # noqa: E402
from dfc.config import DEFAULTS, deep_merge  # noqa: E402
from dfc.engine import Engine, Sample  # noqa: E402
from dfc.policy import AdaptivePoller, Holt, ScalerPolicy, Sentinel, histogram_quantile  # noqa: E402
from dfc.sources import Accumulator, parse_pod_metrics  # noqa: E402


class Act:
    def __init__(self):
        self.calls = []

    def set_replicas(self, n):
        self.calls.append(n)


class T(unittest.TestCase):
    def test_quantile(self):
        b = {0.1: 50, 0.25: 90, 0.5: 100, float("inf"): 100}
        self.assertAlmostEqual(histogram_quantile(0.5, b), 0.1, places=6)
        self.assertTrue(0.1 < histogram_quantile(0.95, b) < 0.5)
        self.assertIsNone(histogram_quantile(0.5, {}))

    def test_holt_tracks_linear_ramp(self):
        h = Holt(0.6, 0.4)
        for t in range(0, 40, 2):
            h.update(t, 10 + 5 * t)
        self.assertAlmostEqual(h.forecast(10), 10 + 5 * 48, delta=40)

    def test_interval_bounds_and_nonlinearity(self):
        p = dict(DEFAULTS["polling"], mode="dynamic")
        ap = AdaptivePoller()
        ap.v_s = ap.a_s = 0.0
        calm = ap.next_interval(50, p)
        self.assertGreater(calm, 20)
        ap2 = AdaptivePoller()
        ap2.v_s, ap2.a_s = 100.0, 50.0
        fast = ap2.next_interval(50, p)
        self.assertLess(fast, 1.5)
        self.assertGreaterEqual(fast, p["min_interval_s"])
        # release: relaxing is gradual
        ap2.v_s = ap2.a_s = 0.0
        nxt = ap2.next_interval(50, p)
        self.assertLess(nxt, p["max_interval_s"] - 5)

    def test_fixed_mode(self):
        p = dict(DEFAULTS["polling"], mode="fixed")
        self.assertEqual(AdaptivePoller().next_interval(100, p), 15.0)

    def test_scaler_up_down_and_stabilisation(self):
        s = ScalerPolicy()
        p = DEFAULTS["scaling"]
        self.assertEqual(s.desired(0, 1, 300, p), 5)           # step limited to +4
        self.assertEqual(s.desired(1, 5, 300, p), 10)          # ceil(300/30)
        self.assertEqual(s.desired(2, 10, 30, p), 10)          # held by stabilisation
        self.assertEqual(s.desired(100, 10, 30, p), 1)         # window elapsed

    def test_accumulator_pod_churn_and_reset(self):
        a = Accumulator()
        a.add("p1", "req", 100)         # first poll: baseline only
        a.finish_poll({"p1"})
        a.add("p1", "req", 130)
        a.add("p2", "req", 20)          # new pod after init: whole counter counts
        a.finish_poll({"p1", "p2"})
        self.assertEqual(a.cum["req"], 50)
        a.add("p1", "req", 5)           # restart -> counter reset
        a.add("p2", "req", 25)
        self.assertEqual(a.cum["req"], 60)

    def test_parse_metrics(self):
        text = '''# TYPE http_requests_total counter
http_requests_total{method="GET",path="/work",status="200"} 90
http_requests_total{method="GET",path="/work",status="500"} 10
http_request_duration_seconds_bucket{le="0.1",path="/work"} 80
http_request_duration_seconds_bucket{le="+Inf",path="/work"} 100
process_cpu_seconds_total 1.5
process_resident_memory_bytes 1000
'''
        m = parse_pod_metrics(text, DEFAULTS["metrics"])
        self.assertEqual(m["req"], 100)
        self.assertEqual(m["err"], 10)
        self.assertEqual(m["buckets"][float("inf")], 100)
        self.assertEqual(m["cpu"], 1.5)

    def test_engine_scales_and_detects(self):
        cfg = deep_merge(DEFAULTS, {"polling": {"mode": "fixed"}})
        act, events = Act(), []
        eng = Engine(lambda: cfg, act, sink=events.append)
        cum, t = 0.0, 0.0
        eng.step(Sample(t=t, cum_req=0))
        for _ in range(4):                    # 10 rps steady
            t += 15
            cum += 150
            eng.step(Sample(t=t, cum_req=cum, spec_replicas=1))
        eng.mark("burst_start", t)
        t += 15
        cum += 15 * 150                       # 150 rps burst
        eng.step(Sample(t=t, cum_req=cum, spec_replicas=1))
        self.assertTrue(any(e["type"] == "detect" for e in events))
        self.assertTrue(any(e["type"] == "first_scale" for e in events))
        self.assertTrue(act.calls and act.calls[-1] > 1)

    def test_sentinel(self):
        s = Sentinel()
        p = DEFAULTS["polling"]
        for i in range(6):
            s.update(i * 2.0, "p", i * 2.0 * 10, 3)       # 10 rps/pod * 3 pods
        self.assertAlmostEqual(s.estimate(10.0, 4.0), 30, delta=1)
        self.assertFalse(Sentinel.triggered(30, 30, p))
        self.assertTrue(Sentinel.triggered(100, 30, p))

    def test_promtext_roundtrip(self):
        r = promtext.Registry()
        r.gauge("g", "x").set(3, a="b")
        r.counter("c").inc(2)
        got = {(n, tuple(sorted(l.items()))): v for n, l, v in promtext.parse(r.render())}
        self.assertEqual(got[("g", (("a", "b"),))], 3.0)
        self.assertEqual(got[("c", ())], 2.0)


if __name__ == "__main__":
    unittest.main()

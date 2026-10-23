#!/usr/bin/env python3
"""Analyse one experiment run, or compare all runs.

  python3 analysis/analyze.py results/<run> [--prom http://localhost:19090] [--plot]
  python3 analysis/analyze.py --compare results

Metric definitions (read these before interpreting numbers):
  detection_delay_s   : controller detect time - (burst_start + time the TRUE load profile first
                        exceeds the detection threshold). Excess delay caused by observation.
  first_scale_delay_s : same reference, until the first scale-up decision.
  time_to_capacity_s  : burst_start -> ready replicas >= ceil(peak/target)   (Prometheus, 5s grid)
  slo_violation_s     : seconds with p95 > SLO or 5xx ratio > SLO            (Prometheus, 5s grid;
                        deliberately NOT taken from controller polls, which are config-dependent)
  forecast_mae        : |forecast - TRUE profile rate at ts+horizon|, burst window
  overhead_*          : scrapes, bytes, polls (controller counters) + controller CPU (Prometheus)
"""
import argparse
import csv
import glob
import json
import math
import os
import statistics as st
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profiles  # noqa: E402


def load_events(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def prom_range(base, q, start, end, step=5):
    url = f"{base}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": q, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(url, timeout=20) as r:
        res = json.load(r)["data"]["result"]
    return res[0]["values"] if res else []


def prom_scalar(base, q, at):
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": q, "time": at})
    with urllib.request.urlopen(url, timeout=20) as r:
        res = json.load(r)["data"]["result"]
    return float(res[0]["value"][1]) if res else None


def analyse(run_dir, prom=None):
    meta = json.load(open(os.path.join(run_dir, "meta.json")))
    cfg = json.load(open(os.path.join(run_dir, "config.json")))
    ev = load_events(os.path.join(run_dir, "events.jsonl"))
    scen, prm = meta["scenario"], meta["params"]
    pts, marks = profiles.points(scen, base=prm["base"], peak=prm["peak"])
    polls = [e for e in ev if e["type"] == "poll"]
    mk = next((e for e in ev if e["type"] == "mark" and e["name"] == "burst_start"), None)
    det = next((e for e in ev if e["type"] == "detect"), None)
    fs = next((e for e in ev if e["type"] == "first_scale"), None)
    scales = [e for e in ev if e["type"] == "scale"]
    S = {"run": meta["run"], "config": meta["config"], "scenario": scen, "rep": meta["rep"]}

    if mk:
        trk = cfg["tracking"]
        thr = max(prm["base"] * trk["detect_ratio"], prm["base"] + trk["detect_abs"])
        t_cross = profiles.crossing_time(pts, thr) - marks["burst_start"]
        T0 = mk["ts"]
        S["detection_delay_s"] = (det["ts"] - T0 - t_cross) if det else None
        S["first_scale_delay_s"] = (fs["ts"] - T0 - t_cross) if fs else None
        # forecast accuracy against the true profile
        errs = []
        for p in polls:
            if p.get("forecast") is not None and p.get("forecast_on") and p["ts"] >= T0:
                tt = p["ts"] + p["forecast_h"] - T0 + marks["burst_start"]
                if tt <= marks["burst_end"] + 10:
                    errs.append(abs(p["forecast"] - profiles.rate_at(pts, tt)))
        S["forecast_mae_rps"] = st.mean(errs) if errs else None
    else:
        T0 = polls[0]["ts"] if polls else meta["start"]

    S["scale_actions"] = len(scales)
    S["peak_replicas"] = max([p["spec"] for p in polls] + [0])
    S["polls"] = len(polls)
    dur = (polls[-1]["ts"] - polls[0]["ts"]) if len(polls) > 1 else 0
    S["polls_per_min"] = len(polls) / dur * 60 if dur else None
    S["mean_interval_s"] = st.mean(p["interval"] for p in polls) if polls else None
    S["overhead_scrapes"] = sum(p.get("scrapes", 0) for p in polls)
    S["overhead_scrape_kib"] = sum(p.get("scrape_bytes", 0) for p in polls) / 1024
    S["sentinel_triggers"] = sum(1 for e in ev if e["type"] == "sentinel_trigger")

    if prom:
        try:
            s, e = meta["start"], meta["end"]
            need = math.ceil(prm["peak"] / cfg["scaling"]["target_per_pod"])
            ready = prom_range(prom, 'kube_deployment_status_replicas_ready{deployment="demo-app"}', s, e)
            b0 = T0 if mk else s
            S["time_to_capacity_s"] = next((float(t) - b0 for t, v in ready
                                            if float(t) >= b0 and float(v) >= need), None)
            app = 'namespace="autoscale-lab",pod=~"demo-app.*"'
            p95 = prom_range(prom, f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{app}}}[10s])))', s, e)
            er = dict(prom_range(prom, f'sum(rate(http_requests_total{{{app},status=~"5.."}}[10s])) / clamp_min(sum(rate(http_requests_total{{{app}}}[10s])),1)', s, e))
            slo = cfg["slo"]
            viol = 0
            for t, v in p95:
                v = float(v)
                if (not math.isnan(v) and v > slo["p95_s"]) or float(er.get(t, 0)) > slo["error_ratio"]:
                    viol += 1
            S["slo_violation_s"] = viol * 5
            win = int(e - s)
            S["overhead_controller_cpu_s"] = prom_scalar(
                prom, f'increase(process_cpu_seconds_total{{namespace="autoscale-lab",pod=~"dfc-controller.*"}}[{win}s])', e)
            S["overhead_probes"] = prom_scalar(prom, f"increase(dfc_sentinel_probes_total[{win}s])", e)
        except Exception as ex:  # Prometheus optional
            print("  (prometheus queries skipped:", ex, ")")

    # client-side view from k6 (independent of any controller setting)
    try:
        k = json.loads(open(os.path.join(run_dir, "k6_summary.json")).read().splitlines()[-1])["metrics"]
        S["k6_p95_ms"] = k["http_req_duration"]["values"]["p(95)"]
        S["k6_failed_ratio"] = k["http_req_failed"]["values"]["rate"]
        S["k6_dropped_iterations"] = k.get("dropped_iterations", {}).get("values", {}).get("count", 0)
    except Exception:
        pass

    json.dump(S, open(os.path.join(run_dir, "summary.json"), "w"), indent=1)
    return S, polls, scales, mk


def plot(run_dir, S, polls, scales, mk):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t0 = polls[0]["ts"]
    x = [p["ts"] - t0 for p in polls]
    fig, ax = plt.subplots(4, 1, figsize=(10, 9), sharex=True)
    ax[0].plot(x, [p.get("rate") for p in polls], ".-", label="observed rps")
    ax[0].plot(x, [p.get("forecast") for p in polls], "--", label="forecast")
    ax[0].legend(); ax[0].set_ylabel("rps")
    ax[1].step(x, [p["interval"] for p in polls], where="post"); ax[1].set_ylabel("next poll interval (s)")
    ax[2].step(x, [p["spec"] for p in polls], where="post", label="spec")
    ax[2].step(x, [p["ready"] for p in polls], where="post", label="ready"); ax[2].legend()
    ax[2].set_ylabel("replicas")
    ax[3].plot(x, [p.get("p95") for p in polls]); ax[3].set_ylabel("p95 (s)"); ax[3].set_xlabel("s")
    if mk:
        for a in ax:
            a.axvline(mk["ts"] - t0, color="r", alpha=.4)
    fig.suptitle(f"{S['config']} / {S['scenario']} / rep {S['rep']}")
    fig.tight_layout()
    fig.savefig(os.path.join(run_dir, "timeline.png"), dpi=110)


def fmt(v):
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def compare(root):
    rows = [json.load(open(p)) for p in glob.glob(os.path.join(root, "*", "summary.json"))]
    if not rows:
        print("no summaries found")
        return
    keys = ["detection_delay_s", "first_scale_delay_s", "time_to_capacity_s", "slo_violation_s",
            "forecast_mae_rps", "polls", "overhead_scrapes", "overhead_controller_cpu_s", "k6_p95_ms"]
    groups = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["config"]), []).append(r)
    print(f"{'scenario':<9}{'cfg':<4}{'n':>3}" + "".join(f"{k[:18]:>20}" for k in keys))
    out = []
    for (sc, cf), rs in sorted(groups.items()):
        vals = {}
        for k in keys:
            xs = [r[k] for r in rs if r.get(k) is not None]
            vals[k] = st.mean(xs) if xs else None
        print(f"{sc:<9}{cf:<4}{len(rs):>3}" + "".join(f"{fmt(vals[k]):>20}" for k in keys))
        out.append({"scenario": sc, "config": cf, "n": len(rs), **vals})
    with open(os.path.join(root, "comparison.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["scenario", "config", "n"] + keys)
        w.writeheader(); w.writerows(out)
    print("wrote", os.path.join(root, "comparison.csv"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--prom")
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--compare", metavar="RESULTS_DIR")
    a = ap.parse_args()
    if a.compare:
        return compare(a.compare)
    S, polls, scales, mk = analyse(a.run_dir, a.prom)
    for k, v in S.items():
        print(f"  {k:<28}{fmt(v)}")
    if a.plot or True:
        try:
            plot(a.run_dir, S, polls, scales, mk)
        except Exception as e:
            print("  (plot skipped:", e, ")")


if __name__ == "__main__":
    main()

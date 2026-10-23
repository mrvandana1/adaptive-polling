#!/usr/bin/env python3
"""Generate manifests/60-grafana-dashboard.yaml (auto-loaded by the Grafana sidecar)."""
import json
import os

NS = 'namespace="autoscale-lab"'
APP = f'{NS},pod=~"demo-app.*"'

# (title, unit, [(expr, legend)])
PANELS = [
    ("Request rate (rps)", "reqps", [
        (f"sum(rate(http_requests_total{{{APP}}}[15s]))", "Prometheus rate(15s)"),
        ("dfc_observed_rate", "DFC observed"),
        ("dfc_forecast_rate", "DFC forecast (horizon)")]),
    ("Response time (s)", "s", [
        (f"histogram_quantile({q}, sum by (le) (rate(http_request_duration_seconds_bucket{{{APP}}}[15s])))", f"p{int(q*100)}")
        for q in (0.5, 0.95, 0.99)]),
    ("Error ratio", "percentunit", [
        (f'sum(rate(http_requests_total{{{APP},status=~"5.."}}[15s])) / clamp_min(sum(rate(http_requests_total{{{APP}}}[15s])),1)', "5xx ratio")]),
    ("Replicas", "short", [
        ('kube_deployment_spec_replicas{deployment="demo-app"}', "spec"),
        ('kube_deployment_status_replicas_ready{deployment="demo-app"}', "ready"),
        ("dfc_desired_replicas", "DFC desired")]),
    ("CPU per pod (cAdvisor, cores)", "short", [
        (f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{APP},container="app"}}[15s]))', "{{pod}}")]),
    ("CPU throttling ratio", "percentunit", [
        (f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{APP},container="app"}}[15s])) / sum by (pod) (rate(container_cpu_cfs_periods_total{{{APP},container="app"}}[15s]))', "{{pod}}")]),
    ("Memory working set per pod", "bytes", [
        (f'container_memory_working_set_bytes{{{APP},container="app"}}', "{{pod}}")]),
    ("In-flight requests", "short", [
        (f"sum(http_requests_in_flight{{{APP}}})", "in flight")]),
    ("DFC polling interval & change score", "s", [
        ("dfc_poll_interval_seconds", "interval (s)"),
        ("dfc_change_score", "change score")]),
    ("Velocity dQ/dt and acceleration d2Q/dt2", "short", [
        ("dfc_velocity", "rps/s"), ("dfc_acceleration", "rps/s^2")]),
    ("Monitoring overhead: scrapes/s & bytes/s", "short", [
        ("rate(dfc_scrapes_total[1m])", "pod scrapes/s"),
        ("rate(dfc_sentinel_probes_total[1m])", "sentinel probes/s"),
        ("rate(dfc_scrape_bytes_total[1m])", "bytes/s")]),
    ("Controller own CPU (cores)", "short", [
        (f'rate(process_cpu_seconds_total{{{NS},pod=~"dfc-controller.*"}}[1m])', "controller CPU")]),
    ("Queue depth & throughput (Redis)", "short", [
        ('redis_key_size{key="jobs"}', "redis jobs length"),
        ("dfc_queue_depth", "DFC queue depth"),
        (f'sum(rate(worker_jobs_processed_total{{{NS}}}[15s]))', "jobs/s processed")]),
    ("Redis internals", "short", [
        (f'redis_connected_clients{{{NS}}}', "clients"),
        (f'rate(redis_commands_processed_total{{{NS}}}[15s])', "commands/s"),
        (f'redis_memory_used_bytes{{{NS}}}', "memory bytes")]),
    ("Ready pods (kube-state-metrics)", "short", [
        (f'sum(kube_pod_status_ready{{{APP},condition="true"}})', "ready pods")]),
    ("Last detection latency (s)", "s", [("dfc_last_detection_latency_seconds", "burst -> detect")]),
]


def panel(i, title, unit, targets):
    return {
        "id": i + 1, "type": "timeseries", "title": title,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"refId": chr(65 + j), "expr": e, "legendFormat": l,
                     "datasource": {"type": "prometheus", "uid": "prometheus"}}
                    for j, (e, l) in enumerate(targets)],
    }


dash = {
    "title": "Adaptive polling lab", "uid": "dfc-lab", "schemaVersion": 39, "version": 1,
    "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "tags": ["dfc"],
    "panels": [panel(i, *p) for i, p in enumerate(PANELS)],
}

here = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(here, "..", "manifests", "60-grafana-dashboard.yaml")
body = json.dumps(dash, indent=1).replace("\n", "\n    ")
with open(out, "w") as f:
    f.write(f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: dfc-dashboard
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
data:
  dfc-lab.json: |
    {body}
""")
print("wrote", os.path.normpath(out))

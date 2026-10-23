# Adaptive Polling Lab — predictive autoscaling + dynamic metric polling on Kubernetes

Minimal, swappable prototype for the "Dynamic Frequency Controller" gap (P02 / P08 / P20):
does polling faster *when load accelerates* detect bursts sooner, and does it help forecasting?

```
 k6 load (steady/step/ramp/accel) ──▶ demo-app (N pods, swappable) ◀── worker ◀── Redis queue
                                          │ /metrics                              │ exporter
        ┌─────────────────────────────────┤                                       │
        ▼                                 ▼                                       ▼
  DFC controller (this repo)       Prometheus (5 s scrape, ground truth)  ◀── cAdvisor, kube-state-metrics,
   • fixed OR dynamic polling      Grafana "Adaptive polling lab"             metrics-server, redis_exporter
   • sentinel probe (1 pod)             ▲
   • Holt forecast (optional)           │ dfc_* metrics (interval, accel, overhead…)
   • HPA-style scaler ──▶ patches Deployment scale
```

Configs: **A** reactive+fixed · **B** predictive+fixed · **C** reactive+dynamic · **D** predictive+dynamic
(identical workload and scaling policy; only polling/forecast differ).

## Quick start (needs docker, kind, kubectl, helm)
```bash
make test            # unit tests (no cluster)
make simulate        # offline A-D comparison with the same engine code (no cluster)
make setup           # kind cluster + metrics-server + Prometheus/Grafana + images + workloads
make forward-grafana # http://localhost:3000  admin/admin  → dashboard "Adaptive polling lab"
./scripts/run-experiment.sh D accel 1          # one run → results/<run>/ (summary.json, timeline.png)
./scripts/run-matrix.sh 3                      # A-D × step/ramp/accel × 3 reps, then comparison.csv
```
Sizing: peak 150 rps × 10 ms CPU ≈ 1.5 cores of app work plus k6. Use ≥ 4 CPUs for Docker, or lower
`PEAK_RPS=80`. Repeat each run ≥ 3× — kind on a laptop is noisy.

## What is collected
| Signal | Source | Resolution |
|---|---|---|
| Request rate, errors (5xx), in-flight | app `/metrics` → DFC and Prometheus | DFC: adaptive 1–30 s · Prometheus: 5 s |
| Response time p50/p95/p99 | app histogram | same |
| CPU per pod, CPU throttling, memory | cAdvisor (Prometheus); app `process_*` (DFC) | 5 s / adaptive |
| Replicas spec / ready / pod readiness | kube-state-metrics, Deployment API | 5 s / per poll |
| Kubernetes Metrics API (`kubectl top`) | metrics-server | for HPA comparison (`manifests/optional/hpa-baseline.yaml`) |
| Redis queue depth, clients, memory, cmds/s | redis_exporter (+ DFC `LLEN`) | 5 s / per poll |
| Queue wait & job duration, jobs/s | worker `/metrics` | 5 s |
| Controller internals: interval, velocity, acceleration, score, forecast, desired replicas | `dfc_*` | per poll |
| **Monitoring overhead**: scrapes, bytes, probes, controller CPU | `dfc_scrapes_total`, `dfc_scrape_bytes_total`, `dfc_sentinel_probes_total`, `process_cpu_seconds_total` | continuous |
| Full per-poll record + marks/detect/scale events | `GET /events` (JSONL) | per poll |
| Client-side latency / failures | k6 summary | per run |

SLO violations and time-to-capacity are computed from **Prometheus' fixed 5 s data**, never from the
controller's own polls — otherwise the metric would change with the thing being tested.

## Changing the application while it runs
Three levels, none require touching the monitoring stack, controller image or load tests:

1. **Live behaviour knobs (no redeploy):**
   `kubectl -n autoscale-lab port-forward svc/demo-app 8000:8000`, then
   `curl -XPOST localhost:8000/admin/config -d '{"cpu_ms":40,"sleep_ms":20,"error_rate":0.02,"ballast_mb":64}'`
   (applies to the pod you hit; for all pods set env on the Deployment).
2. **Swap the code/image (rolling, zero-downtime):**
   `make swap APP=apps/my-api OVERRIDE='{"metrics":{"request_counter":"my_requests_total"}}'`
   builds, loads into kind, `kubectl set image`, waits for rollout, and patches the controller's metric names live.
3. **Retune the controller live:** `curl -XPOST localhost:18080/config -d '{"scaling":{"target_per_pod":20},"polling":{"max_interval_s":10}}'`
   (`make forward-controller` first; `POST /config/reset` clears overrides).

**App contract** (anything satisfying this is scraped and scaled automatically):
- Pod labels `app=demo-app` and `autoscale.lab/monitor=true`; a container port named `http` (or `metrics`) serving Prometheus text at `/metrics`.
- A request counter (`http_requests_total`, label `status`), a latency histogram (`http_request_duration_seconds_bucket`), and `process_cpu_seconds_total` / `process_resident_memory_bytes`. Names are configurable under `metrics:` in the controller config; if the app has no request counter set `scaling.metric: cpu`.

## Controller API (port 8080)
`GET /config` · `POST /config` (deep-merge overlay) · `POST /config/reset` · `POST /mark {"name":"burst_start"}` ·
`POST /reset` (clear history between runs) · `GET /events?since=<epoch>` · `GET /state` · `GET /metrics` (port 9100 too)

## How dynamic polling works here
`interval = min + (max − min) / (1 + score^2)`, `score = max(|dQ/dt|/v_ref, |d²Q/dt²|/a_ref)` normalised by the
current rate; the interval drops immediately and relaxes gradually (`release`). Rates use a trailing window
(≥ `rate_window_s`) built from cumulative counters, so they survive pod churn.

**Important design finding (from `make simulate`):** with only the 1–30 s curve, dynamic mode is *worse* than a
fixed 15 s poll on a sudden burst — after a quiet period the poller sits at ~30 s and is blind when the burst starts.
So dynamic mode includes a **sentinel**: a 1-pod probe every 2 s that wakes the full poll when the estimated rate
jumps (`polling.sentinel_*`). Turn it off (`sentinel_interval_s: 0`) to reproduce the pure-curve variant — that
comparison is itself a result worth reporting. Offline simulation (12 seeds, mean excess detection delay, step burst):
A/B ≈ 10 s, C/D without sentinel ≈ 24 s, C/D with sentinel ≈ 3.7 s. Treat as a hypothesis for the cluster to confirm,
not a result: the simulator has a simple pod model and Poisson noise only.

## Layout
```
apps/demo-app/     stdlib app (+ worker) with runtime knobs, Prometheus metrics, readiness delay
controller/dfc/    policy.py (pure math) engine.py (step) sources.py (scrape→cumulative) main.py (loop+API)
manifests/         namespace, redis(+exporter), app, worker, controller(+RBAC+config), PodMonitor, Grafana dashboard
manifests/optional/ stock HPA baseline, KEDA ScaledObject for the worker
loadtest/          k6 scenarios (mirrored by analysis/profiles.py) + Job template
analysis/          analyze.py (per-run + compare), simulate.py, profiles.py
scripts/           setup, build-images, swap-app, run-experiment, run-matrix, gen_dashboard, teardown
```

## Known limitations
- Verified here: unit tests, the demo app running locally, the offline simulation, the analysis pipeline on simulated
  runs, YAML parsing. **Not yet run on a real cluster** (no kind/helm/k6 in the authoring sandbox) — expect small
  fixes on first `make setup`.
- The forecaster is a deliberately simple Holt model (poor on step bursts; MAE is reported). Swap in a better model
  in `policy.py` behind the same `update()/forecast()` interface.
- The controller scrapes pods directly (needed for 1 s resolution); Prometheus at 5 s remains the independent record.
- Scale-up is limited to `max(4, 2×)` per decision and pod startup includes an artificial 5 s delay
  (`STARTUP_DELAY_S`) — both affect time-to-capacity equally across configs.

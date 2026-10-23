#!/usr/bin/env bash
# One-shot lab setup on kind. Needs: docker, kind, kubectl, helm.
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=dfc-lab

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --config kind/kind-config.yaml
kubectl config use-context "kind-$CLUSTER"

echo "== metrics-server (Kubernetes Metrics API) =="
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true

echo "== kube-prometheus-stack (Prometheus, Grafana, kube-state-metrics, cAdvisor) =="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update >/dev/null
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f helm/prometheus-values.yaml --wait --timeout 10m

echo "== images =="
./scripts/build-images.sh

echo "== lab workloads =="
python3 scripts/gen_dashboard.py
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f manifests/
kubectl -n autoscale-lab rollout status deploy/redis deploy/demo-app deploy/dfc-controller --timeout=180s

cat <<MSG

Ready.
  Grafana    : kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80   (admin/admin)
  Controller : kubectl -n autoscale-lab port-forward svc/dfc-controller 18080:8080
  Run a test : ./scripts/run-experiment.sh D accel 1
MSG

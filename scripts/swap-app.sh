#!/usr/bin/env bash
# Replace the running application WITHOUT touching monitoring, controller or load tests.
#   ./scripts/swap-app.sh <app-dir-with-Dockerfile> [metric-overrides-json]
# Example (app exposes my_requests_total + my_latency_seconds):
#   ./scripts/swap-app.sh apps/my-api '{"metrics":{"request_counter":"my_requests_total","latency_histogram":"my_latency_seconds"}}'
# Rolling update keeps the service up (maxUnavailable: 0). Controller config is patched live.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=${1:?app dir}; OVERRIDE=${2:-}
TAG="dev-$(date +%s)"
IMG="$(basename "$DIR"):$TAG"
NS=autoscale-lab

docker build -t "$IMG" "$DIR"
kind load docker-image "$IMG" --name dfc-lab
kubectl -n $NS set image deploy/demo-app app="$IMG"
kubectl -n $NS rollout status deploy/demo-app --timeout=180s

if [ -n "$OVERRIDE" ]; then
  kubectl -n $NS port-forward svc/dfc-controller 18081:8080 >/dev/null 2>&1 & PF=$!
  trap 'kill $PF 2>/dev/null' EXIT; sleep 2
  curl -sf -XPOST localhost:18081/config -d "$OVERRIDE" >/dev/null && echo "controller metric names updated (live)"
  echo "NOTE: persist the override in manifests/30-controller.yaml (ConfigMap) to survive controller restarts."
fi
echo "swapped demo-app -> $IMG"

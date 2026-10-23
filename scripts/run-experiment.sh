#!/usr/bin/env bash
# ./scripts/run-experiment.sh <A|B|C|D> <steady|step|ramp|accel> [rep]
#   A reactive/fixed   B predictive/fixed   C reactive/dynamic   D predictive/dynamic
# Env: BASE_RPS (10) PEAK_RPS (150) MIN_REPLICAS (1)
set -euo pipefail
cd "$(dirname "$0")/.."
CFG=${1:?A|B|C|D}; SCEN=${2:?steady|step|ramp|accel}; REP=${3:-1}
BASE=${BASE_RPS:-10}; PEAK=${PEAK_RPS:-150}; MINR=${MIN_REPLICAS:-1}
NS=autoscale-lab
RUN="$(date +%Y%m%d-%H%M%S)_${CFG}_${SCEN}_r${REP}"
OUT="results/$RUN"; mkdir -p "$OUT"

case $CFG in
  A) OVERLAY='{"polling":{"mode":"fixed"},"forecast":{"enabled":false}}' ;;
  B) OVERLAY='{"polling":{"mode":"fixed"},"forecast":{"enabled":true}}' ;;
  C) OVERLAY='{"polling":{"mode":"dynamic"},"forecast":{"enabled":false}}' ;;
  D) OVERLAY='{"polling":{"mode":"dynamic"},"forecast":{"enabled":true}}' ;;
  *) echo "config must be A|B|C|D"; exit 1 ;;
esac

kubectl -n $NS port-forward svc/dfc-controller 18080:8080 >/dev/null 2>&1 & PF1=$!
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 >/dev/null 2>&1 & PF2=$!
trap 'kill $PF1 $PF2 2>/dev/null || true' EXIT
sleep 3
API=http://localhost:18080

echo "[$RUN] reset to $MINR replica(s), scaler paused"
curl -sf -XPOST $API/config/reset >/dev/null
curl -sf -XPOST $API/config -d '{"scaling":{"enabled":false}}' >/dev/null
kubectl -n $NS scale deploy/demo-app --replicas=$MINR
kubectl -n $NS rollout status deploy/demo-app --timeout=180s
sleep 15

echo "[$RUN] config $CFG, start load"
curl -sf -XPOST $API/config -d "$OVERLAY" >/dev/null
curl -sf -XPOST $API/reset >/dev/null
START=$(date +%s)

kubectl -n $NS delete job k6-run --ignore-not-found >/dev/null
kubectl -n $NS create configmap k6-scripts --from-file=loadtest/scenarios.js --dry-run=client -o yaml | kubectl apply -f - >/dev/null
sed -e "s/__SCENARIO__/$SCEN/" -e "s/__BASE__/$BASE/" -e "s/__PEAK__/$PEAK/" loadtest/k6-job.yaml | kubectl apply -f - >/dev/null
kubectl -n $NS wait --for=condition=complete job/k6-run --timeout=1200s
sleep 10
END=$(date +%s)

kubectl -n $NS logs job/k6-run > "$OUT/k6.log"
grep -h K6_SUMMARY "$OUT/k6.log" | sed 's/^.*K6_SUMMARY //' > "$OUT/k6_summary.json" || true
curl -sf "$API/events?since=$START" > "$OUT/events.jsonl"
curl -sf "$API/config" > "$OUT/config.json"
cat > "$OUT/meta.json" <<JSON
{"run":"$RUN","config":"$CFG","scenario":"$SCEN","rep":$REP,"start":$START,"end":$END,
 "params":{"base":$BASE,"peak":$PEAK}}
JSON

python3 analysis/analyze.py "$OUT" --prom http://localhost:19090 || true
echo "[$RUN] done -> $OUT"

#!/usr/bin/env bash
# Full comparison: every config x scenario x N repetitions (A-D, step/ramp/accel). ~6 min per run.
set -euo pipefail
cd "$(dirname "$0")/.."
REPS=${1:-3}
for rep in $(seq 1 "$REPS"); do
  for scen in step ramp accel; do
    for cfg in A B C D; do ./scripts/run-experiment.sh $cfg $scen $rep; done
  done
done
python3 analysis/analyze.py --compare results

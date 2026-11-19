#!/usr/bin/env bash
# Runs each fault scenario in SDN mode and in static mode, several repeats each.
# Each run restarts the controller so its event log is clean.
#
#   sudo ./run_validation.sh                  # all scenarios, 3 repeats, results/ directory
#   sudo REPEATS=1 FAULTS="down" ./run_validation.sh
#
# Fault names:  none | loss60 (netem, needs sch_netem) | meter7 | meter3 | down
set -euo pipefail
cd "$(dirname "$0")"
OUT=${OUT:-results}
REPEATS=${REPEATS:-3}
DURATION=${DURATION:-60}
FAULT_AT=${FAULT_AT:-15}
FAULTS=${FAULTS:-"none meter7 meter3 down"}
MODES=${MODES:-"sdn static"}

fault_args() {
  case "$1" in
    none)   echo "--fault none" ;;
    loss60) echo "--fault loss --loss 60" ;;
    meter7) echo "--fault meter --meter-pps 7" ;;
    meter3) echo "--fault meter --meter-pps 3" ;;
    down)   echo "--fault down" ;;
    *) echo "unknown fault $1" >&2; exit 2 ;;
  esac
}

for mode in $MODES; do
  for fault in $FAULTS; do
    for r in $(seq 1 "$REPEATS"); do
      dir="$OUT/${mode}_${fault}_r${r}"
      rm -rf "$dir"; mkdir -p "$dir"
      static=0; [ "$mode" = static ] && static=1
      pkill -f run_controller.py 2>/dev/null || true; sleep 1
      mn -c >/dev/null 2>&1 || true              # clean up BEFORE the controller starts (mn -c kills controller processes)
      PATHGUARD_STATIC=$static PATHGUARD_EVENTS="$dir/controller_events.jsonl" \
        python3 run_controller.py > "$dir/controller.out" 2>&1 &
      ctl=$!; sleep 3
      python3 run_experiment.py --out "$dir" $(fault_args "$fault") \
        --duration "$DURATION" --fault-at "$FAULT_AT" > "$dir/runner.out" 2>&1 || echo "run failed: $dir"
      kill "$ctl" 2>/dev/null || true; wait "$ctl" 2>/dev/null || true
      python3 analyze.py "$dir" > "$dir/analysis.txt" 2>&1 || echo "analysis failed: $dir"
      echo "done $dir"
    done
  done
done
pkill -f run_controller.py 2>/dev/null || true
python3 summarize.py "$OUT"

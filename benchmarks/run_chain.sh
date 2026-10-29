#!/bin/bash
# Runs every suite used in the study, in order. Resumable: finished runs are skipped,
# so this can simply be re-launched after an interruption (benchmarks/runner_ctl.sh start).
cd "$(dirname "$0")/.."
sysctl -qw vm.overcommit_memory=1 2>/dev/null || true
python3 benchmarks/run_all.py sentinel latency latency_trace churn effort distribution persist observer \
    rehash_stall fixskip churn_revert churn_long cliff bigvalues fork sweep mitigation profile interleave core \
    cliff_versions bytes >> results/raw/log_batch2.txt 2>&1

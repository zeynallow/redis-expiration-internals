# What really happens when millions of Redis keys expire at once

An experimental study of Redis active key expiration (`activeExpireCycle`) on Redis **7.2.17**,
**8.8.4** and **8.10.3**, built from source. It combines source-code analysis, research-only
instrumentation and controlled benchmarks.

**Author:** Zeynalabdin ([@zeynallow](https://github.com/zeynallow))

* Technical report: [`reports/technical_report.md`](reports/technical_report.md)
* Findings with evidence: [`analysis/findings.md`](analysis/findings.md)
* Source-code notes (file/function/line/commit): [`analysis/source_code_notes.md`](analysis/source_code_notes.md)
* Hypotheses: [`analysis/hypotheses.md`](analysis/hypotheses.md)
* Upstream issue: [redis/redis#16111](https://github.com/redis/redis/issues/16111) (write-up: [`reports/upstream_issue.md`](reports/upstream_issue.md))

## Key findings (details and evidence in `analysis/findings.md`)

1. **The "10% stale" rule is a cliff at ~30–35%.** With ≤12% of TTL keys expired, active expiry
   reclaims tens of keys/s (one cursor pass over 1M keys ≈ 1 hour) while the server sits idle.
   A binomial model of the stop rule predicts 8.8.4's rates within 0.9–1.15× (F1).
2. **Under steady churn, ≤8.8.4 holds ~21% of TTL keys as garbage and oscillates; 8.10.3 holds ~9%.**
   A one-line revert of redis/redis#15412 reproduces 8.8.4 exactly. Long-TTL keys multiply the
   garbage ~4×; persistent keys don't (F2).
3. **New: active expiry stalls at ~1% remaining while the expires dict shrinks.** The plateau is
   predicted within a few keys (1M–8M keys). It never ends with `activerehashing no` and lasts the
   whole BGSAVE on all versions. A one-condition patch fixes it with no measurable CPU cost.
   Reported upstream as [redis/redis#16111](https://github.com/redis/redis/issues/16111) (F5, F8).
4. **Latency during mass expiry is set by `hz`:** p99 ~27 ms at defaults, ~50 ms with effort 10 (no
   faster), 5–8 ms with hz 100 (and faster) (F3). Traffic makes expiry ~2× faster (F4).
5. **Large hashes freed synchronously overrun the 1 ms FAST budget by up to 48×.**
   `lazyfree-lazy-expire yes` fixes it (F6).
6. Method notes: VM host drift up to 1.5× produced two wrong preliminary conclusions, retracted after
   a round-robin re-test (F9). Polling INFO at 100 Hz speeds expiry ~30% (F4).

588 valid runs (+43 discarded and documented in `results/raw/_discarded/`). The study was stopped
before the `core` matrix finished (61/108 runs). See the report's Limitations section.

## Repository layout

```
environment/   setup.sh (builds all binaries), redis.conf (base config for every run)
instrumentation/ apply_expire_trace.py: research-only per-cycle trace patch (DEBUG EXPIRE-TRACE-*)
tools/         loadgen.c: open-loop latency load generator (hiredis)
benchmarks/    common.py (harness), expiration_test.py, latency_test.py, run_all.py (suites)
analysis/      analyze.py + suites.py (raw -> processed CSV + charts), notes, findings
results/raw/   one .json.gz per run (+ .trace.csv for instrumented builds, .lat.csv.gz for latency)
results/processed/  CSV summaries produced by analysis/analyze.py
charts/        PNG charts produced by analysis/analyze.py
reports/       technical report, upstream issue write-up
```

## Reproducing

```bash
bash environment/setup.sh                 # clone + build 7.2.17, 8.8.4, 8.10.3 (+ trace builds), loadgen
```

```bash
python3 benchmarks/run_all.py --list      # list experiment suites
```

```bash
python3 benchmarks/run_all.py cliff       # run one suite (resumable; existing results are skipped)
```

```bash
python3 analysis/analyze.py               # regenerate results/processed and charts
```

A single run can also be started by hand, for example:

```bash
python3 benchmarks/expiration_test.py --build 8.10.3 --keys 1000000 --scenario L --long-frac 0.9 \
    --timeout 60 --out /tmp/x.json.gz
```

Builds are expected in `$REDIS_BIN_DIR` (default `/home/user/src/bin/<build>/redis-server`).
`redis-server` is pinned to CPU 1, the harness to CPU 0 and the load generator to CPUs 2-3
(override with `REDIS_CPU`, `HARNESS_CPU`, `LOADGEN_CPUS`).

## Builds

| name | what | commit |
|---|---|---|
| `7.2.17` | unmodified | `83937c51afb53e1c69661c8e1533e0e19b896fe1` |
| `8.8.4` | unmodified | `bab5911b2e3114b98b6ae1533c1a15a57ba539a8` |
| `8.10.3` | unmodified | `1a2574ce1551d3de6e0b4a56756c0fdbc6659e4f` |
| `<v>-trace` | + per-cycle trace (`instrumentation/`) | same |
| `8.10.3-revert15412-trace` | 8.10.3 with the one-line fix of redis/redis#15412 reverted, + trace | same |

Results from `-trace` builds are labelled as such and are never mixed with unmodified-build results.

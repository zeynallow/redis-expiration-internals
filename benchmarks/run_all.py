#!/usr/bin/env python3
"""Experiment suites. Each suite is a list of expiration_test runs; results go to
results/raw/<suite>/<run-id>.json.gz (+ .trace.csv for *-trace builds).
Runs are strictly sequential (one redis-server at a time, pinned to a core).

  python3 benchmarks/run_all.py --list
  python3 benchmarks/run_all.py core cliff ...
  python3 benchmarks/run_all.py --reps 3 core     # override repetitions

Existing result files are skipped, so a suite can be resumed.
"""
import argparse
import itertools
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import expiration_test  # noqa: E402
import latency_test  # noqa: E402
import churn_test  # noqa: E402

VERSIONS = ["7.2.17", "8.8.4", "8.10.3"]


def R(**kw):
    return kw


def suite_core(reps):
    """H1: simultaneous expiry, N x value size x version (unmodified builds)."""
    # rep-major order: versions alternate within each round so host drift cannot bias version comparisons
    for rep, n, vs, v in itertools.product(range(reps), [10_000, 100_000, 500_000, 1_000_000],
                                           [64, 1024, 4096], VERSIONS):
        yield f"{v}_n{n}_v{vs}_r{rep}", R(build=v, keys=n, value_size=vs, scenario="A",
                                         timeout=180, settle=12)


def suite_distribution(reps):
    """H3: A vs B vs C (1M x 64B, span 10s)."""
    for v, sc, rep in itertools.product(VERSIONS, "ABC", range(reps)):
        yield f"{v}_{sc}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario=sc,
                                   span_ms=10_000, timeout=60, settle=5)


def suite_persist(reps):
    """Scenario E: a share of keys has no TTL at all (1M x 64B)."""
    for v, pf, rep in itertools.product(VERSIONS, [0.0, 0.5, 0.9], range(reps)):
        yield f"{v}_p{pf}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario="E",
                                    persist_frac=pf, timeout=60, settle=3)


def suite_effort(reps):
    """H2: active-expire-effort sweep, idle server (1M x 64B, simultaneous)."""
    for v, e, rep in itertools.product(VERSIONS, [1, 2, 4, 7, 10], range(reps)):
        yield f"{v}_e{e}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario="A",
                                   effort=e, timeout=60, settle=3)


def suite_cliff(reps):
    """H6: share of expiring keys among long-TTL keys (1M TTL keys total)."""
    fracs = [0.05, 0.08, 0.10, 0.11, 0.12, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]
    for v, sf, rep in itertools.product(["8.10.3", "8.8.4"], fracs, range(reps)):
        yield f"{v}_short{sf}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario="L",
                                        long_frac=round(1 - sf, 4), timeout=60, settle=0, poll=1.0)


def suite_cliff_versions(reps):
    """Same cliff point (10% short) on every version + effort/hz as mitigations."""
    for v, (e, hz), rep in itertools.product(VERSIONS, [(1, 10), (10, 10), (1, 100)], range(reps)):
        yield f"{v}_e{e}_hz{hz}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario="L",
                                          long_frac=0.9, effort=e, hz=hz, timeout=60, settle=0, poll=1.0)


def suite_sweep(reps):
    """Full sweep of a 10%-stale dict: 100K TTL keys, wait until all stale keys are gone."""
    for v, rep in itertools.product(["8.10.3"], range(reps)):
        yield f"{v}_r{rep}", R(build=v + "-trace", keys=100_000, value_size=64, scenario="L",
                              long_frac=0.9, timeout=900, settle=0, poll=2.0)


def suite_bytes(reps):
    """Garbage by bytes, not by count: 100K x 4KB keys expire among 1M x 64B long-TTL keys."""
    for v, rep in itertools.product(["7.2.17", "8.10.3"], range(reps)):
        yield f"{v}_r{rep}", R(build=v + "-trace", keys=1_100_000, value_size=4096, long_value_size=64,
                              scenario="L", long_frac=1 / 1.1, timeout=120, settle=0, poll=1.0)


def suite_fork(reps):
    """H6: mass expiry while a BGSAVE child is alive (~60 s), 1M x 1KB keys."""
    for v, bg, rep in itertools.product(VERSIONS, [0, 1], range(reps)):
        kw = R(build=v + "-trace", keys=1_000_000, value_size=1024, scenario="A", timeout=90, settle=5, poll=0.5)
        if bg:
            kw.update(bgsave_lead=1.0, rdb_key_save_delay=60)
        yield f"{v}_bgsave{bg}_r{rep}", kw


def suite_observer(reps):
    """Observer effect: INFO poll interval vs reclamation speed (idle server, 1M x 64B)."""
    for v, poll, rep in itertools.product(["8.8.4", "8.10.3"], [0.01, 0.05, 0.2, 1.0], range(reps)):
        yield f"{v}_poll{poll}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario="A",
                                         poll=poll, timeout=60, settle=1)


def suite_bigvalues(reps):
    """Large composite values: 20K hashes x 1000 fields (hashtable encoding; > hash-max-listpack-entries=512)
    expire together; lazyfree-lazy-expire no/yes."""
    for v, lz, rep in itertools.product(["7.2.17", "8.10.3"], ["no", "yes"], range(reps)):
        yield f"{v}_lazy{lz}_r{rep}", R(build=v + "-trace", keys=20_000, hash_fields=1000, value_size=16,
                                       scenario="A", config=[f"lazyfree-lazy-expire={lz}"],
                                       timeout=60, settle=3, poll=0.5)


def suite_latency(reps):
    """H2/H5: GET latency (10K req/s = 8 conns x 1250, open loop) during expiry of 1M keys; effort/hz/version."""
    conds = [(1, 10, 1_000_000), (10, 10, 1_000_000), (1, 100, 1_000_000), (1, 10, 0)]
    for v, (e, hz, n), rep in itertools.product(VERSIONS, conds, range(reps)):
        yield f"{v}_e{e}_hz{hz}_n{n}_r{rep}", R(_module="latency", build=v, keys=n, effort=e, hz=hz,
                                                 rate=1250, threads=8, pre=5, post=20)


def suite_latency_trace(reps):
    """Same as latency (effort 1, hz 10) on trace builds: cycle activity under load."""
    for v, rep in itertools.product(VERSIONS + ["8.10.3-revert15412"], range(reps)):
        yield f"{v}_r{rep}", R(_module="latency", build=v + "-trace", keys=1_000_000, rate=1250, threads=8,
                              pre=5, post=20)


def suite_churn(reps):
    """Steady state: 5000 SET PX 60000 per second for 240 s (+/- 1M long-TTL keys); stale share at equilibrium."""
    conds = [(1, 0), (1, 1_000_000), (10, 1_000_000)]
    for v, (e, st), rep in itertools.product(VERSIONS, conds, range(reps)):
        if e == 10 and v == "8.8.4":
            continue
        yield f"{v}_e{e}_static{st}_r{rep}", R(_module="churn", build=v + "-trace", rate=625, threads=8,
                                               ttl_ms=60000, duration=240, static_keys=st, effort=e)


def suite_mitigation(reps):
    """Client-side SCAN sweep as a mitigation for the 10% cliff (1M TTL keys, 10% expire)."""
    for v, cnt, rep in itertools.product(["7.2.17", "8.10.3"], [100, 1000], range(reps)):
        yield f"{v}_count{cnt}_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=64, scenario="L",
                                         long_frac=0.9, scan_at=5, scan_count=cnt, timeout=30, settle=0, poll=1.0)


def suite_rehash_stall(reps):
    """H6f: active expiry pauses at ~1% stale while the expires dict is mid-shrink (<1% skip rule)."""
    for v, n, rep in itertools.product(["7.2.17", "8.10.3"], [1_000_000, 2_000_000, 4_000_000, 8_000_000], range(reps)):
        yield f"{v}_n{n}_rehash_yes_r{rep}", R(build=v + "-trace", keys=n, value_size=16, scenario="A",
                                               timeout=180, settle=1, poll=0.5)
    for v, rep in itertools.product(["7.2.17", "8.10.3"], range(reps)):
        yield f"{v}_n1000000_rehash_no_r{rep}", R(build=v + "-trace", keys=1_000_000, value_size=16, scenario="A",
                                                  config=["activerehashing=no"], timeout=60, settle=1, poll=0.5)


def suite_churn_revert(reps):
    """Causality check for #15412: churn on 8.10.3 with only that one line reverted."""
    for b, rep in itertools.product(["8.10.3-revert15412", "8.10.3", "8.8.4"], range(reps)):
        st = 0
        yield f"{b}_e1_static{st}_r{rep}", R(_module="churn", build=b + "-trace",
                                                           rate=625, threads=8, ttl_ms=60000, duration=240,
                                                           static_keys=st, effort=1)


def suite_churn_long(reps):
    """Churn with 1M long-TTL keys for 12 min: true equilibrium / oscillation, incl. #15412 revert."""
    for v, rep in itertools.product(["8.8.4", "8.10.3", "8.10.3-revert15412"], range(reps)):
        yield f"{v}_static1000000_r{rep}", R(_module="churn", build=v + "-trace", rate=625, threads=8,
                                            ttl_ms=60000, duration=720, static_keys=1_000_000, effort=1)


def suite_profile(reps):
    """perf record -g during mass expiry of 1M x 64B keys (unmodified builds; idle server)."""
    for v, e in [("7.2.17", 1), ("7.2.17", 7), ("8.10.3", 1), ("8.10.3", 7)]:
        for rep in range(reps):
            yield f"{v}_e{e}_r{rep}", R(build=v, keys=1_000_000, value_size=64, scenario="A", effort=e,
                                       perf=6, timeout=60, settle=1, poll=1.0)


def suite_fixskip(reps):
    """Evaluate the candidate fix for F5 (instrumentation/fix_skip_rehashing.patch) vs stock 8.10.3."""
    for b, rep in itertools.product(["8.10.3-trace", "8.10.3-fixskip-trace"], range(reps)):
        yield f"{b}_idle_n1M_r{rep}", R(build=b, keys=1_000_000, value_size=64, scenario="A",
                                       timeout=60, settle=1, poll=1.0)
        yield f"{b}_idle_n4M_r{rep}", R(build=b, keys=4_000_000, value_size=16, scenario="A",
                                       timeout=120, settle=1, poll=1.0)
        yield f"{b}_norehash_n1M_r{rep}", R(build=b, keys=1_000_000, value_size=64, scenario="A",
                                           config=["activerehashing=no"], timeout=60, settle=1, poll=1.0)
        yield f"{b}_load_n1M_r{rep}", R(_module="latency", build=b, keys=1_000_000, rate=1250, threads=8,
                                       pre=5, post=20)


def suite_sentinel(reps):
    """Reference run repeated after every environment change (kernel tag in the run id): comparability check."""
    import platform
    k = platform.release().replace(".", "_")
    for rep in range(reps):
        yield f"8.10.3_e1_{k}_r{rep}", R(build="8.10.3-trace", keys=1_000_000, value_size=64, scenario="A",
                                        effort=1, timeout=60, settle=3)


def suite_interleave(reps):
    """Per-key CPU cost comparisons, interleaved round-robin (ABCD ABCD ...) so host drift cannot
    masquerade as a version/effort/patch effect. 1M x 64B, simultaneous, idle."""
    arms = [("7.2.17", 1), ("8.8.4", 1), ("8.10.3", 1), ("8.10.3-fixskip", 1), ("7.2.17", 4), ("7.2.17", 7)]
    for rep in range(reps):
        for b, e in arms:
            yield f"r{rep}_{b}_e{e}", R(build=b + "-trace", keys=1_000_000, value_size=64, scenario="A",
                                        effort=e, timeout=60, settle=1, poll=1.0)


SUITES = {k[len("suite_"):]: f for k, f in globals().items() if k.startswith("suite_")}
DEFAULT_REPS = {"core": 3, "distribution": 5, "persist": 3, "effort": 5, "cliff": 3,
                "cliff_versions": 3, "sweep": 2, "bytes": 3, "fork": 3, "observer": 3, "bigvalues": 3, "latency": 5, "churn": 2, "mitigation": 3, "latency_trace": 2, "rehash_stall": 2, "churn_revert": 2, "churn_long": 2, "profile": 1, "fixskip": 3, "sentinel": 3, "interleave": 6}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suites", nargs="*")
    ap.add_argument("--reps", type=int)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list or not a.suites:
        for k, f in SUITES.items():
            print(f"{k:16s} reps={DEFAULT_REPS.get(k)}  {f.__doc__}")
        return
    mods = {"expiration": expiration_test, "latency": latency_test, "churn": churn_test}
    for name in a.suites:
        runs = list(SUITES[name](a.reps or DEFAULT_REPS.get(name, 3)))
        for i, (rid, kw) in enumerate(runs):
            out = common.RAW / name / f"{rid}.json.gz"
            if out.exists():
                continue
            mod = mods[kw.pop("_module", "expiration")]
            defaults = vars(mod.parser().parse_args(["--out", "x"]))
            args = argparse.Namespace(**{**defaults, **kw, "out": str(out)})
            t = time.monotonic()
            try:
                m = mod.run(args)
                s = m["samples"][-1]
                print(f"[{name} {i+1}/{len(runs)}] {rid}: completed={m.get('completed')} "
                      f"dbsize={s['dbsize']} {m.get('lat_after_T_us', '')} ({time.monotonic()-t:.0f}s)", flush=True)
            except Exception:
                print(f"[{name} {i+1}/{len(runs)}] {rid}: FAILED", flush=True)
                traceback.print_exc()
                (common.RAW / name).mkdir(parents=True, exist_ok=True)
                with open(common.RAW / name / "FAILED.log", "a") as f:
                    f.write(f"{rid}\n{traceback.format_exc()}\n")


if __name__ == "__main__":
    main()

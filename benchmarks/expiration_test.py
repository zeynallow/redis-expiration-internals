#!/usr/bin/env python3
"""One mass-expiration run: load -> wait for logical expiry -> observe physical
removal and memory reclamation. Writes one gzip'd JSON file of raw data.

Scenarios (expiry schedule relative to the logical instant T):
  A  all keys PXAT T                                   (simultaneous)
  B  uniform in [T, T+span]                            (distributed)
  C  normal(T+span/2, span/20), clipped to [T,T+span]  (concentrated)
  E  --persist-frac of keys without TTL, rest at T     (never-expiring mix)
  L  --long-frac of keys with TTL T+24h, rest at T     (long-TTL mix;
     --long-value-size gives the long-TTL keys a different value size)

Example:
  python3 benchmarks/expiration_test.py --build 8.10.3 --keys 1000000 \
      --value-size 64 --scenario A --effort 1 --out results/raw/x.json.gz
"""
import argparse
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def schedule(n, scenario, T, span_ms, persist_frac, long_frac, seed=42):
    rng = np.random.default_rng(seed)
    if scenario == "A":
        e = np.full(n, T, dtype=np.int64)
    elif scenario == "B":
        e = T + rng.integers(0, span_ms + 1, n)
    elif scenario == "C":
        e = T + np.clip(rng.normal(span_ms / 2, span_ms / 20, n), 0, span_ms).astype(np.int64)
    elif scenario == "E":
        e = np.full(n, T, dtype=np.int64)
        e[rng.random(n) < persist_frac] = 0
    elif scenario == "L":
        e = np.full(n, T, dtype=np.int64)
        e[rng.random(n) < long_frac] = T + 24 * 3600 * 1000
    else:
        raise ValueError(scenario)
    return e


def run(a):
    overrides = {"active-expire-effort": a.effort, "hz": a.hz}
    for kv in a.config or []:
        k, v = kv.split("=", 1)
        overrides[k] = v
    meta = {"args": vars(a), "env": common.environment_info(), "mem_before": common.check_no_swap()}
    srv = common.Server(a.build, overrides).start()
    try:
        r = srv.r
        meta["version"] = srv.version()
        meta["config"] = srv.config()
        trace = a.build.endswith("-trace")
        poller = common.Poller(srv.port, srv.pid, a.poll)
        meta["baseline"] = poller.sample()

        # Choose T so that loading surely finishes before it.
        est_load = a.keys / 250000 * (1 + a.value_size / 1024) * max(1, a.hash_fields / 2) + 1
        T = int((time.time() + est_load * 1.5 + a.margin) * 1000)
        sched = schedule(a.keys, a.scenario, T, a.span_ms, a.persist_frac, a.long_frac)
        if a.scenario == "L" and a.long_value_size is not None:
            # long-TTL keys get their own (typically small) value size
            is_long = sched > T + a.span_ms
            load_s = common.load_keys(r, sched[~is_long], a.value_size, prefix="k:")
            load_s += common.load_keys(r, sched[is_long], a.long_value_size, prefix="l:")
        elif a.hash_fields:
            load_s = common.load_hashes(r, sched, a.hash_fields, a.value_size)
        else:
            load_s = common.load_keys(r, sched, a.value_size)
        loaded = poller.sample()
        meta["after_load"] = loaded
        meta["load_seconds"] = load_s
        meta["T_ms"] = T
        slack = T / 1000 - time.time()
        meta["slack_before_T_s"] = slack
        if slack < 0.5 or loaded.get("expired_keys", 0) != 0 or loaded["dbsize"] != a.keys:
            raise RuntimeError(f"invalid run: slack={slack:.2f}s loaded={loaded}")

        expires_total = int((sched > 0).sum())
        remain_target = int(((sched == 0) | (sched > T + a.span_ms)).sum()) if a.scenario in "EL" else 0
        meta["n_with_ttl"] = expires_total
        meta["remain_target"] = remain_target
        if a.scenario in "BC":
            rel = sched - T
            h, edges = np.histogram(rel, bins=np.arange(0, a.span_ms + 11, 10))
            meta["expiry_hist_10ms"] = h.tolist()

        # Let the load-induced rehashing finish and memory settle, then start.
        time.sleep(max(0.0, slack - 1.0))
        if trace:
            r.execute_command("DEBUG", "EXPIRE-TRACE-START", 4_000_000)
        poller.start()
        if a.bgsave_lead:
            # H6: a long-running fork (BGSAVE slowed down by rdb-key-save-delay, microseconds per key)
            common.wait_until_wall(T / 1000 - a.bgsave_lead)
            r.config_set("rdb-key-save-delay", a.rdb_key_save_delay)
            r.bgsave()
        common.wait_until_wall(T / 1000)
        perf = None
        if a.perf:
            perf_out = Path(a.out).with_suffix("").with_suffix(".perf.data")
            perf_out.parent.mkdir(parents=True, exist_ok=True)
            perf = subprocess.Popen(["perf", "record", "-F", "1999", "-g", "-p", str(srv.pid),
                                     "-o", str(perf_out), "--", "sleep", str(a.perf)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.monotonic()
        if a.scan_at:
            # Mitigation: one full client-side SCAN; on a master every visited expired key is deleted.
            time.sleep(a.scan_at)
            ts, cur, calls, lat = time.monotonic(), 0, 0, []
            while True:
                c0 = time.monotonic()
                cur, _ = r.scan(cur, count=a.scan_count)
                lat.append(time.monotonic() - c0)
                calls += 1
                if cur == 0:
                    break
            meta["scan"] = {"started_after_T_s": a.scan_at, "seconds": time.monotonic() - ts, "calls": calls,
                            "count": a.scan_count, "call_p50_ms": float(np.median(lat) * 1000),
                            "call_max_ms": float(np.max(lat) * 1000)}
        done_at = None
        while time.monotonic() - t0 < a.timeout:
            if poller.samples and poller.samples[-1]["dbsize"] <= remain_target and poller.samples[-1]["t_wall"] * 1000 > T + (a.span_ms if a.scenario in "BC" else 0):
                done_at = time.monotonic()
                break
            time.sleep(0.05)
        meta["completed"] = done_at is not None
        if perf:
            perf.wait()
        time.sleep(a.settle)
        poller.stop()
        meta["samples"] = poller.samples
        if trace:
            tp = Path(a.out).with_suffix("").with_suffix(".trace.csv")
            tp.parent.mkdir(parents=True, exist_ok=True)
            tmp = srv.workdir / "trace.csv"
            n = r.execute_command("DEBUG", "EXPIRE-TRACE-DUMP", str(tmp))
            tp.write_bytes(tmp.read_bytes())
            meta["trace_file"] = tp.name
            meta["trace_records"] = n
        meta["latency_history_expire_cycle"] = r.execute_command("LATENCY", "HISTORY", "expire-cycle")
        meta["before_purge"] = poller.sample()
        r.execute_command("MEMORY", "PURGE")
        time.sleep(0.5)
        meta["after_purge"] = poller.sample()
        meta["mem_after"] = common.check_no_swap()
    finally:
        srv.stop()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt") as f:
        json.dump(meta, f, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    return meta


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", default="8.10.3")
    p.add_argument("--keys", type=int, default=100000)
    p.add_argument("--value-size", type=int, default=64)
    p.add_argument("--scenario", default="A", choices=list("ABCEL"))
    p.add_argument("--span-ms", type=int, default=10000)
    p.add_argument("--persist-frac", type=float, default=0.5)
    p.add_argument("--long-frac", type=float, default=0.9)
    p.add_argument("--long-value-size", type=int, default=None,
                   help="scenario L: value size of the long-TTL keys (default: --value-size)")
    p.add_argument("--hash-fields", type=int, default=0,
                   help="if >0, keys are hashes with this many fields of --value-size bytes")
    p.add_argument("--effort", type=int, default=1)
    p.add_argument("--hz", type=int, default=10)
    p.add_argument("--config", action="append", help="extra key=value server config")
    p.add_argument("--bgsave-lead", type=float, default=0, help="start BGSAVE this many s before T")
    p.add_argument("--rdb-key-save-delay", type=int, default=0, help="us per key while saving (slow fork)")
    p.add_argument("--scan-at", type=float, default=0, help="run one full SCAN this many s after T")
    p.add_argument("--scan-count", type=int, default=1000)
    p.add_argument("--perf", type=float, default=0, help="perf record -g the server for N s from T")
    p.add_argument("--poll", type=float, default=0.2, help="poll interval seconds")
    p.add_argument("--margin", type=float, default=3.0)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--settle", type=float, default=12, help="observe memory after completion")
    p.add_argument("--out", required=True)
    return p


if __name__ == "__main__":
    m = run(parser().parse_args())
    s = m["samples"]
    print(f"completed={m['completed']} load={m['load_seconds']:.1f}s samples={len(s)} "
          f"final_dbsize={s[-1]['dbsize']} used_memory={s[-1]['used_memory']/1e6:.1f}MB")

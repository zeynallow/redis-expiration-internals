#!/usr/bin/env python3
"""Steady-state churn: a cache-like workload that keeps writing keys with a fixed TTL.

tools/loadgen writes `threads x rate` SET key val PX ttl per second (open loop)
for `duration` seconds. At steady state the number of *live* keys is
write_rate x ttl, so every key above that in DBSIZE is expired-but-not-reclaimed.
The live count is computed exactly from the loadgen's own per-request log.

  python3 benchmarks/churn_test.py --build 8.10.3 --rate 2500 --threads 2 --ttl-ms 60000 \
      --duration 240 --out results/raw/churn/x.json.gz
"""
import argparse
import gzip
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

LOADGEN = common.ROOT / "tools" / "loadgen"


def run(a):
    overrides = {"active-expire-effort": a.effort, "hz": a.hz}
    for kv in a.config or []:
        k, v = kv.split("=", 1)
        overrides[k] = v
    meta = {"args": vars(a), "env": common.environment_info()}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    srv = common.Server(a.build, overrides).start()
    try:
        r = srv.r
        meta["version"], meta["config"] = srv.version(), srv.config()
        if a.static_keys:  # optional long-lived keys with a long TTL (they also live in db->expires)
            common.load_keys(r, np.full(a.static_keys, int((time.time() + 86400) * 1000), dtype=np.int64),
                             a.value_size, prefix="s:")
        trace = a.build.endswith("-trace")
        if trace:
            r.execute_command("DEBUG", "EXPIRE-TRACE-START", 4_000_000)
        poller = common.Poller(srv.port, srv.pid, a.poll)
        meta["baseline"] = poller.sample()
        poller.start()
        lg_csv = srv.workdir / "lat.csv"
        t_start = time.time()
        subprocess.run(["taskset", "-c", common.LOADGEN_CPUS, str(LOADGEN), "-p", str(srv.port),
                        "-t", str(a.threads), "-r", str(a.rate), "-d", str(a.duration), "-o", str(lg_csv),
                        "-w", "1.0", "-x", str(a.ttl_ms), "-v", str(a.value_size)], check=True)
        poller.stop()
        meta["t_start"] = t_start
        meta["samples"] = poller.samples
        if trace:
            tmp = srv.workdir / "trace.csv"
            meta["trace_records"] = r.execute_command("DEBUG", "EXPIRE-TRACE-DUMP", str(tmp))
            shutil.copy(tmp, out.with_suffix("").with_suffix(".trace.csv"))
        lat = np.loadtxt(lg_csv, delimiter=",", skiprows=1, usecols=(0, 1))
        # write completion time ~= sched + latency; key expires ttl after the server executed it
        done = np.sort((lat[:, 0] + lat[:, 1]) / 1e6)
        for s in poller.samples:
            t = s["t_wall"]
            s["live_keys"] = int(np.searchsorted(done, t) - np.searchsorted(done, t - a.ttl_ms / 1000)) + a.static_keys
            s["stale_keys"] = s["dbsize"] - s["live_keys"]
        meta["lat_us"] = {p: float(np.percentile(lat[:, 1], p)) for p in (50, 95, 99, 99.9, 100)}
        steady = [s for s in poller.samples if s["t_wall"] - t_start > a.ttl_ms / 1000 * 1.5]
        if steady:
            meta["steady_stale_frac_median"] = float(np.median([s["stale_keys"] / s["dbsize"] for s in steady]))
            meta["steady_stale_keys_median"] = float(np.median([s["stale_keys"] for s in steady]))
            meta["steady_used_memory_median"] = float(np.median([s["used_memory"] for s in steady]))
        if a.keep_latency_log:
            with open(lg_csv, "rb") as fi, gzip.open(out.with_suffix("").with_suffix(".lat.csv.gz"), "wb") as fo:
                shutil.copyfileobj(fi, fo)
        # per-second latency percentiles (the full per-request log is large for long runs)
        sec = np.floor(lat[:, 0] / 1e6 - t_start).astype(int)
        meta["lat_per_second_us"] = [
            {"t": int(t), "n": int((sec == t).sum()), "p50": float(np.percentile(lat[sec == t, 1], 50)),
             "p99": float(np.percentile(lat[sec == t, 1], 99)), "max": float(lat[sec == t, 1].max())}
            for t in np.unique(sec)]
    finally:
        srv.stop()
    with gzip.open(out, "wt") as f:
        json.dump(meta, f, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    return meta


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", default="8.10.3")
    p.add_argument("--rate", type=float, default=625, help="writes/s per thread")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--ttl-ms", type=int, default=60000)
    p.add_argument("--duration", type=int, default=240)
    p.add_argument("--value-size", type=int, default=64)
    p.add_argument("--static-keys", type=int, default=0)
    p.add_argument("--effort", type=int, default=1)
    p.add_argument("--hz", type=int, default=10)
    p.add_argument("--config", action="append")
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--keep-latency-log", action="store_true", help="also store every request's latency")
    p.add_argument("--out", required=True)
    return p


if __name__ == "__main__":
    m = run(parser().parse_args())
    print({k: v for k, v in m.items() if k.startswith("steady") or k == "lat_us"})

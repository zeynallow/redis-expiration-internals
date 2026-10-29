#!/usr/bin/env python3
"""H5: mass expiration while the server serves traffic.

Loads --keys short-TTL keys ("k:*", all PXAT T) plus --hot persistent keys
("h:*") and runs tools/loadgen (open loop, GETs on h:* = always hits) from
T - pre to T + post. Records every request latency (raw CSV, gzip) together
with the usual INFO/proc time series.

  python3 benchmarks/latency_test.py --build 8.10.3 --rate 5000 --threads 2 \
      --out results/raw/latency/x.json.gz
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
        poller = common.Poller(srv.port, srv.pid, a.poll)
        common.load_keys(r, np.zeros(a.hot, dtype=np.int64), 64, prefix="h:")
        T = int((time.time() + a.keys / 250000 * (1 + a.value_size / 1024) * 1.5 + a.pre + 3) * 1000)
        meta["load_seconds"] = common.load_keys(r, np.full(a.keys, T, dtype=np.int64), a.value_size)
        meta["T_ms"] = T
        slack = T / 1000 - time.time()
        if slack < a.pre + 0.5:
            raise RuntimeError(f"load too slow, slack={slack}")
        trace = a.build.endswith("-trace")
        if trace:
            r.execute_command("DEBUG", "EXPIRE-TRACE-START", 4_000_000)
        common.wait_until_wall(T / 1000 - a.pre)
        poller.start()
        lg_csv = srv.workdir / "lat.csv"
        lg = None
        if a.rate > 0:
            lg = subprocess.Popen(["taskset", "-c", common.LOADGEN_CPUS, str(LOADGEN), "-p", str(srv.port),
                                   "-t", str(a.threads), "-r", str(a.rate), "-d", str(int(a.pre + a.post)),
                                   "-o", str(lg_csv), "-n", str(a.hot), "-k", "h:", "-w", str(a.write_frac)])
            lg.wait()
        else:
            time.sleep(a.pre + a.post)
        poller.stop()
        meta["samples"] = poller.samples
        meta["latency_history_expire_cycle"] = r.execute_command("LATENCY", "HISTORY", "expire-cycle")
        if trace:
            tmp = srv.workdir / "trace.csv"
            meta["trace_records"] = r.execute_command("DEBUG", "EXPIRE-TRACE-DUMP", str(tmp))
            shutil.copy(tmp, out.with_suffix("").with_suffix(".trace.csv"))
        if lg is not None:
            with open(lg_csv, "rb") as fi, gzip.open(out.with_suffix("").with_suffix(".lat.csv.gz"), "wb") as fo:
                shutil.copyfileobj(fi, fo)
            lat = np.loadtxt(lg_csv, delimiter=",", skiprows=1, usecols=(0, 1))
            rel = lat[:, 0] / 1e6 - T / 1000
            for name, m in (("before", rel < 0), ("after_T", rel >= 0)):
                if m.any():
                    meta[f"lat_{name}_us"] = {p: float(np.percentile(lat[m, 1], p)) for p in (50, 95, 99, 99.9, 100)}
    finally:
        srv.stop()
    with gzip.open(out, "wt") as f:
        json.dump(meta, f, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    return meta


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", default="8.10.3")
    p.add_argument("--keys", type=int, default=1_000_000)
    p.add_argument("--value-size", type=int, default=64)
    p.add_argument("--hot", type=int, default=100_000)
    p.add_argument("--rate", type=float, default=5000, help="requests/s per thread (0 = no load)")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--write-frac", type=float, default=0.0)
    p.add_argument("--effort", type=int, default=1)
    p.add_argument("--hz", type=int, default=10)
    p.add_argument("--config", action="append")
    p.add_argument("--pre", type=float, default=5)
    p.add_argument("--post", type=float, default=20)
    p.add_argument("--poll", type=float, default=0.5)
    p.add_argument("--out", required=True)
    return p


if __name__ == "__main__":
    m = run(parser().parse_args())
    print({k: v for k, v in m.items() if k.startswith("lat_")})

#!/usr/bin/env python3
"""Turn results/raw/** into results/processed/*.csv and charts/*.png.

  python3 analysis/analyze.py            # all suites with data
  python3 analysis/analyze.py cliff core # selected
"""
import csv
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW, PROC, CHARTS = ROOT / "results/raw", ROOT / "results/processed", ROOT / "charts"
PROC.mkdir(parents=True, exist_ok=True)
CHARTS.mkdir(parents=True, exist_ok=True)
COLORS = {"7.2.17": "#4C72B0", "8.8.4": "#DD8452", "8.10.3": "#55A868",
          "8.10.3-revert15412": "#C44E52", "8.10.3-fixskip": "#8172B3"}
plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3, "font.size": 9})


def load(path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def runs(suite):
    for p in sorted((RAW / suite).glob("*.json.gz")):
        if p.name.endswith(".lat.csv.gz"):
            continue
        yield p, load(p)


def trace(path):
    tp = Path(str(path).replace(".json.gz", ".trace.csv"))
    if not tp.exists():
        return None, {}
    with open(tp) as f:
        head = f.readline()
        hdr = dict(kv.split("=") for kv in head[1:].split())
        rows = np.loadtxt(f, delimiter=",", skiprows=1, ndmin=2)
    # cols: start_us,elapsed_us,type,tl_exit,iterations,sampled,expired,expires_left
    return rows, {k: int(v) for k, v in hdr.items()}


def base_version(build):
    return build.replace("-trace", "")


def pct(xs, q):
    return float(np.percentile(xs, q)) if len(xs) else float("nan")


def summarize(vals):
    v = np.asarray([x for x in vals if x == x], dtype=float)
    if not len(v):
        return {"n": 0}
    return {"n": len(v), "median": float(np.median(v)), "min": float(v.min()), "max": float(v.max()),
            "p25": pct(v, 25), "p75": pct(v, 75), "mean": float(v.mean()), "std": float(v.std(ddof=1)) if len(v) > 1 else 0.0}


def reclaim_times_from_samples(m):
    """Seconds after T at which 50/90/99/100% of the expiring keys were physically gone (poll resolution)."""
    T = m["T_ms"] / 1000
    n_exp = m["after_load"]["dbsize"] - m["remain_target"]
    out = {}
    for q in (50, 90, 99, 100):
        thr = m["remain_target"] + n_exp * (1 - q / 100)
        t = next((s["t_wall"] - T for s in m["samples"] if s["t_wall"] >= T and s["dbsize"] <= thr + 1e-9), float("nan"))
        out[f"t{q}"] = t
    return out


def reclaim_times_from_trace(rows, m):
    """Same, but exact (cycle granularity) from the instrumented trace."""
    T_us = m["T_ms"] * 1000
    left0 = m["after_load"]["dbsize"] - (m["after_load"]["dbsize"] - m["n_with_ttl"])  # keys in expires
    n_exp = m["n_with_ttl"] - (m["remain_target"] - (m["after_load"]["dbsize"] - m["n_with_ttl"]))
    out = {}
    end_us = rows[:, 0] + rows[:, 1]
    for q in (50, 90, 99, 100):
        thr = left0 - n_exp * q / 100
        idx = np.where((rows[:, 7] <= thr + 1e-9) & (end_us >= T_us))[0]
        out[f"t{q}"] = (end_us[idx[0]] - T_us) / 1e6 if len(idx) else float("nan")
    return out


# ----------------------------------------------------------------------------------------------
def write_csv(name, rows):
    if not rows:
        return
    keys = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(PROC / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print("wrote", PROC / name)


if __name__ == "__main__":
    import suites  # noqa: E402  (analysis/suites.py)
    wanted = sys.argv[1:] or suites.ALL
    for s in wanted:
        if (RAW / s).exists():
            getattr(suites, s)()

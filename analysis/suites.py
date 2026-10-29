"""Per-suite analyses (called from analyze.py)."""
import gzip
import re
from pathlib import Path
from collections import defaultdict
from math import comb

import matplotlib.pyplot as plt
import numpy as np

from analyze import (CHARTS, COLORS, PROC, base_version, reclaim_times_from_samples,
                     reclaim_times_from_trace, runs, summarize, trace, write_csv, RAW, load)

ALL = ["core", "distribution", "persist", "effort", "cliff", "cliff_epoch_v180", "cliff_versions", "sweep", "bytes",
       "latency", "observer", "fork", "bigvalues", "churn", "latency_trace", "churn_revert", "churn_long",
       "rehash_stall", "mitigation", "profile", "fixskip", "interleave"]


def stale_series(m, rows):
    """(t_rel_s, stale_keys) from the trace: keys still in expires minus keys that must remain."""
    T_us = m["T_ms"] * 1000
    keep_in_expires = m["remain_target"] - (m["after_load"]["dbsize"] - m["n_with_ttl"])
    t = (rows[:, 0] + rows[:, 1] - T_us) / 1e6
    return t, rows[:, 7] - keep_in_expires


def repeat_prob(p, n=20, thr=10):
    """P(a 20-key sample has expired*100/n > thr) for stale fraction p."""
    k_min = (thr * n) // 100 + 1
    return sum(comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(k_min, n + 1))


# ----------------------------------------------------------------------------------------------
def cliff(suite="cliff"):
    rows_out = []
    series = defaultdict(list)
    for p, m in runs(suite):
        tr, hdr = trace(p)
        sf = round(1 - m["args"]["long_frac"], 4)
        n_exp = m["n_with_ttl"] - (m["remain_target"] - (m["after_load"]["dbsize"] - m["n_with_ttl"]))
        t, stale = stale_series(m, tr)
        win = (t >= 0) & (t <= 60)
        end_stale = stale[win][-1]
        # measured reclaim rate during 10..60 s (steady state)
        w2 = (t >= 10) & (t <= 60)
        rate = (stale[w2][0] - stale[w2][-1]) / (t[w2][-1] - t[w2][0]) if w2.sum() > 2 else float("nan")
        cyc = tr[(tr[:, 0] >= m["T_ms"] * 1000 + 10e6) & (tr[:, 0] <= m["T_ms"] * 1000 + 60e6)]
        rows_out.append({"run": p.stem.replace(".json", ""), "version": base_version(m["args"]["build"]),
                         "short_frac": sf, "n_expiring": n_exp,
                         "stale_after_60s": int(end_stale), "reclaimed_pct_60s": 100 * (1 - end_stale / n_exp),
                         "t100_s": next((float(tt) for tt, s in zip(t, stale) if tt >= 0 and s <= 0), float("nan")),
                         "rate_10_60s_keys_per_s": rate,
                         "slow_cycles_per_s": (cyc[:, 2] == 0).sum() / 50, "fast_cycles_per_s": (cyc[:, 2] == 1).sum() / 50,
                         "sampled_per_s": cyc[:, 5].sum() / 50,
                         "fast_gate_rejects": hdr.get("fast_gate_rejects"),
                         "stale_perc_est_end": m["samples"][-1].get("expired_stale_perc")})
        series[sf].append((t, stale / n_exp * 100, base_version(m["args"]["build"])))
    write_csv(f"{suite}_runs.csv", rows_out)
    agg = []
    for v, sf in sorted({(r["version"], r["short_frac"]) for r in rows_out}):
        rs = [r for r in rows_out if r["short_frac"] == sf and r["version"] == v]
        s = summarize([r["reclaimed_pct_60s"] for r in rs])
        t1 = summarize([r["t100_s"] for r in rs])
        rate = summarize([r["rate_10_60s_keys_per_s"] for r in rs])
        agg.append({"version": v, "short_frac": sf, "n": s["n"], "reclaimed_pct_60s_median": s["median"], "min": s["min"],
                    "max": s["max"], "t100_median_s": t1.get("median"), "steady_rate_median": rate.get("median"),
                    "model_repeat_prob": repeat_prob(sf)})
    write_csv(f"{suite}_summary.csv", agg)

    fig, ax = plt.subplots(1, 3, figsize=(15, 3.8))
    for v in ("8.8.4", "8.10.3"):
        av = [a for a in agg if a["version"] == v]
        if not av:
            continue
        x = [a["short_frac"] * 100 for a in av]
        y = [a["reclaimed_pct_60s_median"] for a in av]
        lo = [a["reclaimed_pct_60s_median"] - a["min"] for a in av]
        hi = [a["max"] - a["reclaimed_pct_60s_median"] for a in av]
        ax[0].errorbar(x, y, yerr=[lo, hi], marker="o", capsize=3, color=COLORS[v], label=v)
    ax[0].legend(fontsize=7)
    ax[0].axvline(10, ls="--", color="gray")
    ax[0].text(10.3, 50, "ACTIVE_EXPIRE_CYCLE_\nACCEPTABLE_STALE = 10%", fontsize=7, color="gray")
    ax[0].set_xlabel("expiring keys as % of all keys with a TTL")
    ax[0].set_ylabel("% of expired keys physically removed\n60 s after expiry")
    ax[0].set_title("Reclamation cliff (1M TTL keys, effort 1, hz 10, idle)")
    cmap = plt.get_cmap("viridis")
    for i, sf in enumerate(sorted(series)):
        cand = [ts for ts in series[sf] if ts[2] == "8.10.3"] or series[sf]
        t, s, _ = cand[0]
        ax[1].plot(t, s, label=f"{sf*100:g}%", color=cmap(i / max(1, len(series) - 1)))
    ax[1].set_xlim(-1, 60)
    ax[1].set_xlabel("seconds after logical expiry")
    ax[1].set_ylabel("expired keys still in memory (%)")
    ax[1].legend(title="expiring share", fontsize=7, ncol=2)
    ax[1].set_title("Stale keys over time (8.10.3, rep 0)")
    # model: slow cycles only (10/s), batch of 20, repeat if >10% of the batch expired
    ps = np.linspace(0.02, 0.5, 200)
    model = [min(10 * 20 / max(1e-9, 1 - repeat_prob(pp)) * pp, 1e6) for pp in ps]
    ax[2].plot(ps * 100, model, color="gray", ls="--", label="model: 10 slow cycles/s,\n20-key batches, repeat if >10%")
    for v in ("8.8.4", "8.10.3"):
        rs = [r for r in rows_out if r["version"] == v and r["rate_10_60s_keys_per_s"] == r["rate_10_60s_keys_per_s"]]
        if rs:
            ax[2].plot([r["short_frac"] * 100 for r in rs], [r["rate_10_60s_keys_per_s"] for r in rs], "o",
                       color=COLORS[v], label=f"{v} measured", alpha=0.7)
    ax[2].set_yscale("log"); ax[2].set_xlabel("expiring keys as % of keys with a TTL")
    ax[2].set_ylabel("reclaim rate, keys/s (10-60 s after T)")
    ax[2].legend(fontsize=7); ax[2].set_title("Measured vs model")
    fig.tight_layout()
    fig.savefig(CHARTS / f"{suite}.png")
    print(f"chart {suite}.png")



def cliff_epoch_v180():
    cliff("cliff_epoch_v180")

def cliff_versions():
    out = []
    for p, m in runs("cliff_versions"):
        tr, hdr = trace(p)
        n_exp = m["n_with_ttl"] - (m["remain_target"] - (m["after_load"]["dbsize"] - m["n_with_ttl"]))
        t, stale = stale_series(m, tr)
        end = stale[(t >= 0) & (t <= 60)][-1]
        a = m["args"]
        T_us = m["T_ms"] * 1000
        win = (tr[:, 0] >= T_us) & (tr[:, 0] <= T_us + 60e6)
        out.append({"version": base_version(a["build"]), "effort": a["effort"], "hz": a["hz"],
                    "reclaimed_pct_60s": 100 * (1 - end / n_exp),
                    "t100_s": next((float(tt) for tt, s in zip(t, stale) if tt >= 0 and s <= 0), float("nan")),
                    "expire_cpu_ms_60s": tr[win, 1].sum() / 1000,
                    "max_cycle_ms": tr[win, 1].max() / 1000})
    write_csv("cliff_versions_runs.csv", out)
    agg = []
    for key in sorted({(r["version"], r["effort"], r["hz"]) for r in out}):
        rs = [r for r in out if (r["version"], r["effort"], r["hz"]) == key]
        agg.append({"version": key[0], "effort": key[1], "hz": key[2], "n": len(rs),
                    "reclaimed_pct_60s_median": summarize([r["reclaimed_pct_60s"] for r in rs])["median"],
                    "reclaimed_min": summarize([r["reclaimed_pct_60s"] for r in rs])["min"],
                    "reclaimed_max": summarize([r["reclaimed_pct_60s"] for r in rs])["max"],
                    "t100_median_s": summarize([r["t100_s"] for r in rs]).get("median"),
                    "expire_cpu_ms_60s_median": summarize([r["expire_cpu_ms_60s"] for r in rs])["median"],
                    "max_cycle_ms_median": summarize([r["max_cycle_ms"] for r in rs])["median"]})
    write_csv("cliff_versions_summary.csv", agg)


def bytes():
    out = []
    fig, ax = plt.subplots(figsize=(6, 3.4))
    for p, m in runs("bytes"):
        T = m["T_ms"] / 1000
        s = m["samples"]
        t = np.array([x["t_wall"] - T for x in s])
        um = np.array([x["used_memory"] for x in s]) / 2**20
        db = np.array([x["dbsize"] for x in s])
        base = m["baseline"]["used_memory"] / 2**20
        v = base_version(m["args"]["build"])
        # logical live dataset = the 1M long-TTL keys; measure it from a separate probe below
        out.append({"run": p.name, "version": v, "used_memory_MiB_after_load": m["after_load"]["used_memory"] / 2**20,
                    "used_memory_MiB_T+60": float(um[np.searchsorted(t, 60)]) if t[-1] > 60 else float("nan"),
                    "used_memory_MiB_end": float(um[-1]), "t_end": float(t[-1]),
                    "stale_keys_end": int(db[-1] - m["remain_target"]),
                    "rss_MiB_end": s[-1]["proc_rss_kb"] / 1024})
        if p.name.endswith("_r0.json.gz"):
            ax.plot(t, um, label=f"{v} used_memory", color=COLORS[v])
    ax.set_xlabel("seconds after 100K x 4KB keys expired")
    ax.set_ylabel("MiB")
    ax.set_xlim(-2, None)
    ax.set_title("1M live 64B keys + 100K expired 4KB keys")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(CHARTS / "bytes_garbage.png")
    write_csv("bytes_runs.csv", out)


# ----------------------------------------------------------------------------------------------
def _trace_cycle_stats(tr, m, t_from=0.0, t_to=None):
    T_us = m["T_ms"] * 1000
    st = (tr[:, 0] - T_us) / 1e6
    w = (st >= t_from) & ((st <= t_to) if t_to is not None else True)
    c = tr[w]
    out = {}
    for typ, name in ((0, "slow"), (1, "fast")):
        x = c[c[:, 2] == typ]
        out[f"{name}_cycles"] = int(len(x))
        out[f"{name}_ms_total"] = float(x[:, 1].sum() / 1000)
        out[f"{name}_max_ms"] = float(x[:, 1].max() / 1000) if len(x) else 0.0
        out[f"{name}_p50_ms"] = float(np.median(x[:, 1]) / 1000) if len(x) else 0.0
        out[f"{name}_expired"] = int(x[:, 6].sum())
    return out


def _group(rows, keys, metrics):
    agg = []
    for key in sorted({tuple(r[k] for k in keys) for r in rows}, key=lambda t: tuple(str(x) for x in t)):
        rs = [r for r in rows if tuple(r[k] for k in keys) == key]
        d = dict(zip(keys, key))
        d["n"] = len(rs)
        for mname in metrics:
            s = summarize([r.get(mname, float("nan")) for r in rs])
            d[f"{mname}_median"] = s.get("median")
            d[f"{mname}_min"] = s.get("min")
            d[f"{mname}_max"] = s.get("max")
        agg.append(d)
    return agg


def core():
    rows = []
    for p, m in runs("core"):
        a = m["args"]
        rt = reclaim_times_from_samples(m)
        s = m["samples"]
        T = m["T_ms"] / 1000
        base = m["baseline"]
        load = m["after_load"]
        end = m["before_purge"]
        # time for used_memory to come within 1% (of the loaded delta) of its final value
        um = np.array([x["used_memory"] for x in s]); t = np.array([x["t_wall"] - T for x in s])
        delta = load["used_memory"] - end["used_memory"]
        idx = np.where((t >= 0) & (um - end["used_memory"] <= 0.01 * delta))[0]
        cpu = lambda x: x.get("used_cpu_user_main_thread", 0) + x.get("used_cpu_sys_main_thread", 0)
        s_after = [x for x in s if x["t_wall"] >= T]
        rows.append({"version": base_version(a["build"]), "keys": a["keys"], "value_size": a["value_size"],
                     "rep": p.name, **rt, "rate_keys_per_s": a["keys"] / rt["t100"] if rt["t100"] == rt["t100"] and rt["t100"] > 0 else float("nan"),
                     "t_mem99_s": float(t[idx[0]]) if len(idx) else float("nan"),
                     "used_memory_loaded_MiB": load["used_memory"] / 2**20,
                     "used_memory_end_MiB": end["used_memory"] / 2**20,
                     "rss_loaded_MiB": load["proc_rss_kb"] / 1024, "rss_end_MiB": end["proc_rss_kb"] / 1024,
                     "rss_after_purge_MiB": m["after_purge"]["proc_rss_kb"] / 1024,
                     "allocator_resident_end_MiB": end.get("allocator_resident", float("nan")) / 2**20,
                     "allocator_active_end_MiB": end.get("allocator_active", float("nan")) / 2**20,
                     "frag_ratio_end": end.get("mem_fragmentation_ratio"),
                     "expire_cpu_ms": end["expire_cycle_cpu_milliseconds"] - load["expire_cycle_cpu_milliseconds"],
                     "main_thread_cpu_s": cpu(s_after[-1]) - cpu(s_after[0]) if s_after else float("nan"),
                     "vcsw": s_after[-1].get("proc_vcsw", 0) - s_after[0].get("proc_vcsw", 0) if s_after else 0,
                     "nvcsw": s_after[-1].get("proc_nvcsw", 0) - s_after[0].get("proc_nvcsw", 0) if s_after else 0,
                     "time_cap_hits": end["expired_time_cap_reached_count"] - load["expired_time_cap_reached_count"]})
    write_csv("core_runs.csv", rows)
    agg = _group(rows, ["version", "keys", "value_size"],
                 ["t50", "t90", "t100", "rate_keys_per_s", "t_mem99_s", "used_memory_loaded_MiB", "used_memory_end_MiB",
                  "rss_loaded_MiB", "rss_end_MiB", "rss_after_purge_MiB", "expire_cpu_ms", "main_thread_cpu_s"])
    write_csv("core_summary.csv", agg)
    fig, ax = plt.subplots(1, 3, figsize=(12, 3.5), sharey=True)
    for i, vs in enumerate([64, 1024, 4096]):
        for v in COLORS:
            pts = [(r["keys"], r["t100_median"], r["t100_min"], r["t100_max"]) for r in agg
                   if r["version"] == v and r["value_size"] == vs]
            if not pts:
                continue
            pts.sort()
            x, y, lo, hi = map(np.array, zip(*pts))
            ax[i].errorbar(x, y, yerr=[y - lo, hi - y], marker="o", capsize=3, label=v, color=COLORS[v])
        ax[i].set_xscale("log"); ax[i].set_title(f"value {vs} B")
        ax[i].set_xlabel("keys expiring at the same ms")
    ax[0].set_ylabel("s until all expired keys removed\n(idle server, median, min-max)")
    ax[0].legend()
    fig.tight_layout(); fig.savefig(CHARTS / "core_reclaim_time.png")
    # memory gap chart for 1M keys
    fig, ax = plt.subplots(figsize=(7, 3.5))
    width = 0.25
    sub = [r for r in agg if r["keys"] == 1_000_000]
    labels = sorted({(r["value_size"]) for r in sub})
    for j, v in enumerate([v for v in COLORS if any(r["version"] == v for r in sub)]):
        for k, (metric, hatch) in enumerate([("rss_end_MiB", ""), ("rss_after_purge_MiB", "//")]):
            ys = [next((r[f"{metric}_median"] for r in sub if r["version"] == v and r["value_size"] == vs), np.nan) for vs in labels]
            ax.bar(np.arange(len(labels)) + j * width + (k * width / 2) - width / 4, ys, width / 2,
                   color=COLORS[v], hatch=hatch, label=f"{v} {'RSS 12 s after' if k == 0 else 'RSS after MEMORY PURGE'}")
    ax.set_xticks(np.arange(len(labels)) + width); ax.set_xticklabels([f"{x} B" for x in labels])
    ax.set_ylabel("MiB (used_memory is ~1-2 MiB)")
    ax.set_title("RSS retained after 1M keys expired (dataset empty)")
    ax.legend(fontsize=6)
    fig.tight_layout(); fig.savefig(CHARTS / "core_rss_retained.png")


def effort():
    rows = []
    for p, m in runs("effort"):
        tr, _ = trace(p)
        a = m["args"]
        rt = reclaim_times_from_trace(tr, m)
        cs = _trace_cycle_stats(tr, m, 0, rt["t100"] + 0.01 if rt["t100"] == rt["t100"] else None)
        rows.append({"version": base_version(a["build"]), "effort": a["effort"], **rt, **cs,
                     "expire_cpu_s": (cs["slow_ms_total"] + cs["fast_ms_total"]) / 1000,
                     "duty_pct": 100 * (cs["slow_ms_total"] + cs["fast_ms_total"]) / 1000 / rt["t100"]})
    write_csv("effort_runs.csv", rows)
    agg = _group(rows, ["version", "effort"], ["t100", "expire_cpu_s", "duty_pct", "slow_max_ms", "fast_max_ms",
                                               "slow_cycles", "fast_cycles"])
    write_csv("effort_summary.csv", agg)
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.5))
    for v in COLORS:
        pts = sorted((r["effort"], r["t100_median"], r["slow_max_ms_median"]) for r in agg if r["version"] == v)
        if not pts:
            continue
        x, y, mx = zip(*pts)
        ax[0].plot(x, y, marker="o", label=v, color=COLORS[v])
        ax[1].plot(x, mx, marker="o", label=v, color=COLORS[v])
    ax[0].set_xlabel("active-expire-effort"); ax[0].set_ylabel("s to remove 1M expired keys (idle)")
    ax[1].set_xlabel("active-expire-effort"); ax[1].set_ylabel("longest single expire cycle (ms)")
    ax[0].legend(); fig.tight_layout(); fig.savefig(CHARTS / "effort.png")


def distribution():
    rows = []
    fig, ax = plt.subplots(figsize=(7, 3.5))
    for p, m in runs("distribution"):
        tr, _ = trace(p)
        a = m["args"]
        T_us = m["T_ms"] * 1000
        t = (tr[:, 0] + tr[:, 1] - T_us) / 1e6
        left = tr[:, 7]
        # logical remaining at time t from the expiry histogram (10 ms bins)
        if "expiry_hist_10ms" in m:
            cum = np.cumsum(m["expiry_hist_10ms"])
            logical = a["keys"] - cum[np.clip((t * 100).astype(int), 0, len(cum) - 1)]
            logical[t < 0] = a["keys"]
        else:
            logical = np.where(t < 0, a["keys"], 0)
        stale = left - logical
        w = t >= 0
        rows.append({"version": base_version(a["build"]), "scenario": a["scenario"], "rep": p.name,
                     "max_stale_keys": float(stale[w].max()), "mean_stale_keys_0_12s": float(stale[w & (t <= 12)].mean()),
                     **reclaim_times_from_trace(tr, m),
                     **{k: v for k, v in _trace_cycle_stats(tr, m, 0, 15).items() if k.endswith(("max_ms", "cycles", "ms_total"))}})
        if p.name.endswith("_r0.json.gz") and base_version(a["build"]) == "8.10.3":
            ax.plot(t[w], stale[w] / 1e3, label=f"{a['scenario']}: physical - logical")
    ax.set_xlim(0, 15); ax.set_xlabel("s after first logical expiry"); ax.set_ylabel("expired keys still present (K)")
    ax.set_title("8.10.3, 1M keys: A=same ms, B=uniform 10 s, C=normal(5 s, 0.5 s)")
    ax.legend(); fig.tight_layout(); fig.savefig(CHARTS / "distribution.png")
    write_csv("distribution_runs.csv", rows)
    write_csv("distribution_summary.csv", _group(rows, ["version", "scenario"],
                                                  ["max_stale_keys", "mean_stale_keys_0_12s", "t100", "slow_max_ms", "fast_max_ms"]))


def persist():
    rows = []
    for p, m in runs("persist"):
        tr, _ = trace(p)
        a = m["args"]
        rows.append({"version": base_version(a["build"]), "persist_frac": a["persist_frac"],
                     "n_expiring": m["n_with_ttl"], **reclaim_times_from_trace(tr, m)})
    write_csv("persist_runs.csv", rows)
    write_csv("persist_summary.csv", _group(rows, ["version", "persist_frac"], ["n_expiring", "t100"]))


def observer():
    rows = []
    for p, m in runs("observer"):
        tr, hdr = trace(p)
        a = m["args"]
        rt = reclaim_times_from_trace(tr, m)
        cs = _trace_cycle_stats(tr, m, 0, rt["t100"])
        rows.append({"version": base_version(a["build"]), "poll_s": a["poll"], **rt, **cs})
    write_csv("observer_runs.csv", rows)
    agg = _group(rows, ["version", "poll_s"], ["t100", "fast_cycles", "fast_ms_total", "slow_ms_total"])
    write_csv("observer_summary.csv", agg)
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    for v in ("8.8.4", "8.10.3"):
        pts = sorted((r["poll_s"], r["t100_median"], r["t100_min"], r["t100_max"]) for r in agg if r["version"] == v)
        if pts:
            x, y, lo, hi = map(np.array, zip(*pts))
            ax.errorbar(1 / x, y, yerr=[y - lo, hi - y], marker="o", capsize=3, label=v, color=COLORS[v])
    ax.set_xscale("log"); ax.set_xlabel("INFO polls per second (monitoring only)")
    ax.set_ylabel("s to remove 1M expired keys"); ax.legend(); ax.set_title("Observer effect")
    fig.tight_layout(); fig.savefig(CHARTS / "observer.png")


def bigvalues():
    rows = []
    for p, m in runs("bigvalues"):
        tr, _ = trace(p)
        a = m["args"]
        lazy = [c for c in a["config"] if c.startswith("lazyfree")][0].split("=")[1]
        rt = reclaim_times_from_trace(tr, m)
        cs = _trace_cycle_stats(tr, m, 0, rt["t100"] + 0.01)
        T_us = m["T_ms"] * 1000
        c = tr[(tr[:, 0] >= T_us) & (tr[:, 6] > 0)]
        fast = c[c[:, 2] == 1]
        rows.append({"version": base_version(a["build"]), "lazyfree_lazy_expire": lazy, **rt, **cs,
                     "fast_over_budget_pct": float(100 * (fast[:, 1] > 1000).mean()) if len(fast) else float("nan"),
                     "fast_p99_ms": float(np.percentile(fast[:, 1], 99) / 1000) if len(fast) else float("nan"),
                     "lazyfreed_objects": m["before_purge"].get("lazyfreed_objects")})
    write_csv("bigvalues_runs.csv", rows)
    write_csv("bigvalues_summary.csv", _group(rows, ["version", "lazyfree_lazy_expire"],
                                               ["t100", "slow_max_ms", "fast_max_ms", "fast_p50_ms", "fast_p99_ms",
                                                "fast_over_budget_pct"]))


def fork():
    rows = []
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.5))
    for p, m in runs("fork"):
        tr, _ = trace(p)
        a = m["args"]
        v = base_version(a["build"])
        bg = a["bgsave_lead"] > 0
        T = m["T_ms"] / 1000
        s = m["samples"]
        t = np.array([x["t_wall"] - T for x in s])
        db = np.array([x["dbsize"] for x in s])
        bgs = np.array([x.get("rdb_bgsave_in_progress", 0) for x in s])
        cow = np.array([x.get("child_private_dirty_kb", 0) or 0 for x in s]) / 1024
        rss = np.array([x["proc_rss_kb"] for x in s]) / 1024
        in_bg = (t >= 0) & (bgs > 0)
        rows.append({"version": v, "bgsave": int(bg), "rep": p.name,
                     "stalled_keys_during_bgsave": int(np.median(db[in_bg][-5:])) if in_bg.sum() > 5 else 0,
                     "bgsave_seconds_after_T": float(t[in_bg].max()) if in_bg.any() else 0.0,
                     "t100_s": next((float(x) for x, d in zip(t, db) if x >= 0 and d == 0), float("nan")),
                     "max_child_private_dirty_MiB": float(cow.max()),
                     "max_parent_plus_child_private_MiB": float((rss + cow).max()),
                     "parent_rss_at_T_MiB": float(rss[np.searchsorted(t, 0)])})
        if p.name.endswith("_r0.json.gz") and bg:
            ax[0].plot(t, db / 1e3, label=v, color=COLORS[v])
            ax[1].plot(t, rss + cow, label=f"{v} parent RSS + child private", color=COLORS[v])
    ax[0].set_yscale("symlog", linthresh=1); ax[0].set_xlabel("s after expiry (BGSAVE running ~60 s)")
    ax[0].set_ylabel("keys still present (K)"); ax[0].legend(); ax[0].set_xlim(-2, None)
    ax[1].set_xlabel("s after expiry"); ax[1].set_ylabel("MiB"); ax[1].legend(fontsize=7); ax[1].set_xlim(-2, None)
    fig.tight_layout(); fig.savefig(CHARTS / "fork.png")
    write_csv("fork_runs.csv", rows)
    write_csv("fork_summary.csv", _group(rows, ["version", "bgsave"], ["stalled_keys_during_bgsave", "t100_s",
                                                                       "max_child_private_dirty_MiB", "max_parent_plus_child_private_MiB"]))


def sweep():
    rows = []
    fig, ax = plt.subplots(figsize=(6, 3.4))
    for p, m in runs("sweep"):
        tr, _ = trace(p)
        t, stale = stale_series(m, tr)
        n_exp = stale[t <= 0][-1] if (t <= 0).any() else stale[0]
        t100 = next((float(x) for x, s in zip(t, stale) if x >= 0 and s <= 0), float("nan"))
        rows.append({"run": p.name, "n_ttl_keys": m["n_with_ttl"], "n_expired": int(m["n_with_ttl"] - m["remain_target"]),
                     "t50": next((float(x) for x, s in zip(t, stale) if x >= 0 and s <= n_exp / 2), float("nan")),
                     "t100": t100})
        ax.plot(t, stale, label=p.name.split(".json")[0])
    ax.set_xlabel("s after expiry"); ax.set_ylabel("expired keys still present")
    ax.set_title("100K TTL keys, 10% expire at once (8.10.3)"); ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(CHARTS / "sweep.png")
    write_csv("sweep_runs.csv", rows)


def _lat_windows(lat_path, T_ms, t100):
    lat = np.loadtxt(gzip.open(lat_path, "rt"), delimiter=",", skiprows=1, usecols=(0, 1))
    rel = lat[:, 0] / 1e6 - T_ms / 1000
    out = {}
    wins = {"before": (rel < 0), "during": (rel >= 0) & (rel < t100), "after": (rel >= t100 + 1)}
    for name, msk in wins.items():
        x = lat[msk, 1]
        if len(x):
            for q in (50, 95, 99, 99.9):
                out[f"{name}_p{q}_us"] = float(np.percentile(x, q))
            out[f"{name}_max_us"] = float(x.max())
            out[f"{name}_n"] = int(len(x))
    return out, rel, lat[:, 1]


def latency():
    import gzip as _gz  # noqa
    rows = []
    figdata = {}
    for p, m in runs("latency"):
        a = m["args"]
        T = m["T_ms"] / 1000
        s = m["samples"]
        if a["keys"]:
            t100 = next((x["t_wall"] - T for x in s if x["t_wall"] >= T and x["dbsize"] <= a["hot"]), float("nan"))
        else:
            t100 = float("nan")  # no mass expiry: compare against the same 0-5 s window
        lp = Path(str(p).replace(".json.gz", ".lat.csv.gz"))
        w, rel, lat = _lat_windows(lp, m["T_ms"], t100 if t100 == t100 else 5.0)
        rows.append({"version": base_version(a["build"]), "effort": a["effort"], "hz": a["hz"], "keys": a["keys"],
                     "rep": p.name, "t100_s": t100, **w})
        if p.name.endswith("_r0.json.gz"):
            figdata[(base_version(a["build"]), a["effort"], a["hz"], a["keys"])] = (rel, lat)
    write_csv("latency_runs.csv", rows)
    agg = _group(rows, ["version", "effort", "hz", "keys"],
                 ["t100_s", "before_p99_us", "during_p50_us", "during_p99_us", "during_p99.9_us", "during_max_us",
                  "after_p99_us"])
    write_csv("latency_summary.csv", agg)
    fig, axs = plt.subplots(2, 2, figsize=(11, 6), sharey=True)
    for ax, key in zip(axs.flat, [("8.10.3", 1, 10, 1_000_000), ("8.10.3", 10, 10, 1_000_000),
                                  ("8.10.3", 1, 100, 1_000_000), ("8.8.4", 1, 10, 1_000_000)]):
        if key not in figdata:
            continue
        rel, lat = figdata[key]
        ax.scatter(rel, lat / 1000, s=1, alpha=0.3, color=COLORS[key[0]], rasterized=True)
        ax.set_yscale("log"); ax.set_title(f"{key[0]} effort={key[1]} hz={key[2]}")
        ax.set_xlabel("s relative to expiry of 1M keys"); ax.set_ylabel("GET latency (ms)")
    fig.suptitle("Per-request GET latency, 10K req/s open loop (unmodified builds)")
    fig.tight_layout(); fig.savefig(CHARTS / "latency_scatter.png", dpi=110)


def churn(suite="churn"):
    rows = []
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for p, m in runs(suite):
        a = m["args"]
        v = base_version(a["build"])
        tr, hdr = trace(p)
        s = m["samples"]
        t0 = m["t_start"]
        # steady-state window: second half of the run (and at least 1.5 x TTL after start)
        t_steady = max(a["ttl_ms"] / 1000 * 1.5, a["duration"] / 2)
        steady = [x for x in s if x["t_wall"] - t0 > t_steady]
        ttl_keys = [x["db0_expires"] if "db0_expires" in x else x["dbsize"] for x in steady]
        frac_ttl = [x["stale_keys"] / e for x, e in zip(steady, ttl_keys)]
        st_us = (tr[:, 0] / 1e6 - t0)
        sw = st_us > t_steady
        span = st_us[sw].max() - st_us[sw].min() if sw.any() else 1
        rows.append({"version": v, "effort": a["effort"], "static_keys": a["static_keys"], "rep": p.name,
                     "duration_s": a["duration"],
                     "stale_keys_median": float(np.median([x["stale_keys"] for x in steady])),
                     "stale_keys_min": float(np.min([x["stale_keys"] for x in steady])),
                     "stale_keys_max": float(np.max([x["stale_keys"] for x in steady])),
                     "stale_keys_peak_whole_run": float(np.max([x["stale_keys"] for x in s])),
                     "stale_share_of_ttl_keys_median": float(np.median(frac_ttl)),
                     "stale_perc_est_median": float(np.median([x.get("expired_stale_perc", np.nan) for x in steady])),
                     "used_memory_MiB_median": float(np.median([x["used_memory"] for x in steady])) / 2**20,
                     "fast_cycles_per_s": float((tr[sw, 2] == 1).sum() / span),
                     "slow_cycles_per_s": float((tr[sw, 2] == 0).sum() / span),
                     "expire_cpu_pct": float(100 * tr[sw, 1].sum() / 1e6 / span),
                     "lat_p99_us": m["lat_us"]["99"], "lat_p99.9_us": m["lat_us"]["99.9"], "lat_max_us": m["lat_us"]["100"]})
        if p.name.endswith("_r0.json.gz") and (a["static_keys"] or suite == "churn_revert") and a["effort"] == 1:
            ax.plot([x["t_wall"] - t0 for x in s], [x["stale_keys"] / 1e3 for x in s], label=v, color=COLORS[v])
    ax.set_xlabel("s since start of churn (5000 SET PX 60000 /s" + ("; 1M long-TTL keys present)" if suite != "churn_revert" else ")"))
    ax.set_ylabel("expired keys still in memory (K)")
    ax.axhline(0, color="k", lw=0.5); ax.legend(); fig.tight_layout(); fig.savefig(CHARTS / f"{suite}.png")
    write_csv(f"{suite}_runs.csv", rows)
    write_csv(f"{suite}_summary.csv", _group(rows, ["version", "effort", "static_keys", "duration_s"],
                                          ["stale_keys_median", "stale_keys_min", "stale_keys_max", "stale_keys_peak_whole_run", "stale_share_of_ttl_keys_median", "stale_perc_est_median",
                                           "used_memory_MiB_median", "fast_cycles_per_s", "expire_cpu_pct",
                                           "lat_p99_us", "lat_p99.9_us", "lat_max_us"]))


def churn_revert():
    churn("churn_revert")


def churn_long():
    churn("churn_long")


def _plateaus(tr, m, min_s=0.15):
    """Intervals after T where keys remain in expires but cycles make no progress."""
    T_us = m["T_ms"] * 1000
    a = tr[tr[:, 0] >= T_us]
    t = (a[:, 0] - T_us) / 1e6
    left = a[:, 7]
    out, i = [], 0
    while i < len(left):
        j = i
        while j + 1 < len(left) and left[j + 1] == left[i]:
            j += 1
        if left[i] > 0 and t[j] - t[i] >= min_s:
            out.append((float(t[i]), float(t[j] + a[j, 1] / 1e6), int(left[i])))
        i = j + 1
    return out


def rehash_stall():
    rows = []
    for p, m in runs("rehash_stall"):
        tr, _ = trace(p)
        a = m["args"]
        rehash = "no" if any("activerehashing=no" in c for c in (a.get("config") or [])) else "yes"
        pl = _plateaus(tr, m)
        main = max(pl, key=lambda x: x[1] - x[0]) if pl else (float("nan"), float("nan"), 0)
        rt = reclaim_times_from_trace(tr, m)
        n = a["keys"]
        exp_size = 1 << (n - 1).bit_length()
        rows.append({"version": base_version(a["build"]), "keys": n, "activerehashing": rehash, "rep": p.name,
                     "plateau_keys": main[2], "plateau_start_s": main[0], "plateau_len_s": main[1] - main[0],
                     "pred_threshold_keys": (exp_size + exp_size // 8) // 100,
                     "t100_s": rt["t100"], "n_plateaus": len(pl),
                     "completed": m["completed"], "final_dbsize": m["samples"][-1]["dbsize"]})
    write_csv("rehash_stall_runs.csv", rows)
    write_csv("rehash_stall_summary.csv", _group(rows, ["version", "keys", "activerehashing"],
                                                 ["plateau_keys", "pred_threshold_keys", "plateau_len_s", "t100_s"]))
    fig, ax = plt.subplots(figsize=(6, 3.4))
    for v in ("7.2.17", "8.10.3"):
        pts = sorted((r["keys"], r["plateau_len_s"]) for r in rows if r["version"] == v and r["activerehashing"] == "yes")
        if pts:
            x, y = zip(*pts)
            ax.plot(np.array(x) / 1e6, y, "o", color=COLORS[v], label=v)
    ax.set_xlabel("keys expiring at once (millions)"); ax.set_ylabel("stall length (s)")
    ax.set_title("Active expiry stall at ~1% remaining (dict mid-shrink)"); ax.legend()
    fig.tight_layout(); fig.savefig(CHARTS / "rehash_stall.png")


def mitigation():
    rows = []
    for p, m in runs("mitigation"):
        tr, _ = trace(p)
        a = m["args"]
        t, stale = stale_series(m, tr)
        sc = m["scan"]
        before = stale[t <= a["scan_at"]][-1]
        # the run stops as soon as DBSIZE reaches the live count, so use the last poll sample
        after = m["samples"][-1]["dbsize"] - m["remain_target"]
        rows.append({"version": base_version(a["build"]), "scan_count": a["scan_count"], "rep": p.name,
                     "stale_before_scan": int(before), "stale_after_scan": int(after),
                     "scan_seconds": sc["seconds"], "scan_calls": sc["calls"],
                     "scan_call_p50_ms": sc["call_p50_ms"], "scan_call_max_ms": sc["call_max_ms"]})
    write_csv("mitigation_runs.csv", rows)
    write_csv("mitigation_summary.csv", _group(rows, ["version", "scan_count"],
                                               ["stale_before_scan", "stale_after_scan", "scan_seconds",
                                                "scan_call_p50_ms", "scan_call_max_ms"]))


def latency_trace():
    rows = []
    for p, m in runs("latency_trace"):
        tr, hdr = trace(p)
        T = m["T_ms"] * 1000
        c = tr[(tr[:, 0] >= T) & (tr[:, 6] > 0)]
        t_end = (c[-1, 0] + c[-1, 1] - T) / 1e6
        fast, slow = c[c[:, 2] == 1], c[c[:, 2] == 0]
        pl = _plateaus(tr, m)
        rows.append({"run": p.name.replace(".json.gz", ""), "build": m["args"]["build"], "t100_s": t_end,
                     "slow_cycles": len(slow), "slow_ms": slow[:, 1].sum() / 1e3, "slow_max_ms": slow[:, 1].max() / 1e3,
                     "fast_cycles": len(fast), "fast_ms": fast[:, 1].sum() / 1e3, "fast_max_ms": fast[:, 1].max() / 1e3,
                     "fast_per_s": len(fast) / t_end, "duty_pct": 100 * c[:, 1].sum() / 1e6 / t_end,
                     "fast_share_of_deletions_pct": 100 * fast[:, 6].sum() / c[:, 6].sum(),
                     "fast_gate_rejects": hdr.get("fast_gate_rejects"),
                     "plateau_keys": pl[0][2] if pl else 0, "plateau_len_s": (pl[0][1] - pl[0][0]) if pl else 0.0})
    write_csv("latency_trace_runs.csv", rows)


def profile():
    """Top self-time symbols from perf.data recorded during the expiry window."""
    import subprocess
    rows = []
    for pd in sorted((RAW / "profile").glob("*.perf.data")):
        rid = pd.name.replace(".perf.data", "")
        rep = subprocess.run(["perf", "report", "-i", str(pd), "--stdio", "--no-children", "--sort", "symbol",
                              "--percent-limit", "0.5"], capture_output=True, text=True).stdout
        (PROC.parent / "processed" / f"profile_{rid}.txt").write_text(rep)
        for line in rep.splitlines():
            mm = re.match(r"\s+([\d.]+)%\s+\[\.\]\s+(\S+)", line)
            if mm:
                rows.append({"run": rid, "self_pct": float(mm.group(1)), "symbol": mm.group(2)})
    write_csv("profile_top_symbols.csv", rows)


def fixskip():
    rows = []
    for p, m in runs("fixskip"):
        tr, _ = trace(p)
        a = m["args"]
        name = p.name.replace(".json.gz", "")
        cond = name.split("-trace_")[1].rsplit("_r", 1)[0]
        T = m["T_ms"] * 1000
        c = tr[(tr[:, 0] >= T) & (tr[:, 6] > 0)]
        t100 = (c[-1, 0] + c[-1, 1] - T) / 1e6 if len(c) else float("nan")
        left_end = int(tr[-1, 7])
        pl = _plateaus(tr, m)
        row = {"build": base_version(a["build"]), "condition": cond, "rep": name,
               "t100_s": t100 if left_end == 0 else float("nan"), "keys_left_at_end": left_end,
               "longest_stall_s": max([b - x for x, b, k in pl], default=0.0),
               "expire_cpu_ms": float(c[:, 1].sum() / 1000), "max_cycle_ms": float(c[:, 1].max() / 1000)}
        lp = Path(str(p).replace(".json.gz", ".lat.csv.gz"))
        if lp.exists():
            w, _, _ = _lat_windows(lp, m["T_ms"], t100 if t100 == t100 else 5.0)
            row.update({k: v for k, v in w.items() if k.startswith("during_") or k.startswith("before_p99")})
        rows.append(row)
    write_csv("fixskip_runs.csv", rows)
    write_csv("fixskip_summary.csv", _group(rows, ["build", "condition"],
                                            ["t100_s", "keys_left_at_end", "longest_stall_s", "expire_cpu_ms",
                                             "max_cycle_ms", "during_p99_us", "during_max_us"]))


def interleave():
    rows = []
    for p, m in runs("interleave"):
        tr, _ = trace(p)
        a = m["args"]
        T = m["T_ms"] * 1000
        c = tr[(tr[:, 0] >= T) & (tr[:, 6] > 0)]
        rnd = int(p.name.split("_")[0][1:])
        rows.append({"round": rnd, "arm": f'{base_version(a["build"])} e{a["effort"]}',
                     "us_per_key": float(c[:, 1].sum() / c[:, 6].sum()),
                     "t100_s": float((c[-1, 0] + c[-1, 1] - T) / 1e6),
                     "expire_cpu_ms": float(c[:, 1].sum() / 1000), "start": m["T_ms"]})
    write_csv("interleave_runs.csv", rows)
    agg = _group(rows, ["arm"], ["us_per_key", "t100_s", "expire_cpu_ms"])
    # paired comparison against 8.10.3 e1 within each round (removes between-round drift)
    base = {r["round"]: r for r in rows if r["arm"] == "8.10.3 e1"}
    for a in agg:
        ratios = [r["us_per_key"] / base[r["round"]]["us_per_key"] for r in rows if r["arm"] == a["arm"] and r["round"] in base]
        a["paired_ratio_vs_8.10.3_median"] = float(np.median(ratios)) if ratios else float("nan")
        a["paired_ratio_min"] = float(np.min(ratios)) if ratios else float("nan")
        a["paired_ratio_max"] = float(np.max(ratios)) if ratios else float("nan")
    write_csv("interleave_summary.csv", agg)

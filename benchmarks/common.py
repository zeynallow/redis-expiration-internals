"""Shared harness: server lifecycle, dataset loading, metric polling.

Design notes (see README "Methodology"):
  * Every run starts a *fresh* redis-server process (clean allocator state).
  * Data is loaded server-side with Lua (EVAL batches) so that the client is
    never the bottleneck; every key gets an explicit absolute PXAT so that the
    logical expiration instant is known exactly.
  * The poller only uses INFO / DBSIZE / /proc. None of these touch individual
    keys, so they never trigger *passive* expiration. Every command does however
    wake the event loop (and thus beforeSleep -> fast expire cycle); this
    observer effect is quantified separately (benchmarks/observer_effect.py).
  * Wall-clock (time.time) is used only to align with PXAT; all durations are
    measured with time.monotonic().
"""
import json
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import redis

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(os.environ.get("REDIS_BIN_DIR", "/home/user/src/bin"))
RAW = ROOT / "results" / "raw"
PORT = int(os.environ.get("REDIS_PORT", "6399"))
REDIS_CPU = os.environ.get("REDIS_CPU", "1")      # core redis-server is pinned to
HARNESS_CPU = os.environ.get("HARNESS_CPU", "0")  # core this harness is pinned to
LOADGEN_CPUS = os.environ.get("LOADGEN_CPUS", "2,3")

BASE_CONF = ROOT / "environment" / "redis.conf"

INFO_FIELDS = [
    "used_memory", "used_memory_rss", "used_memory_dataset", "mem_fragmentation_ratio",
    "allocator_allocated", "allocator_active", "allocator_resident", "allocator_frag_ratio",
    "allocator_frag_bytes", "allocator_rss_bytes", "rss_overhead_bytes",
    "expired_keys", "expired_keys_active", "expired_stale_perc", "expired_time_cap_reached_count",
    "expire_cycle_cpu_milliseconds", "keyspace_hits", "keyspace_misses", "evicted_keys",
    "used_cpu_sys", "used_cpu_user", "used_cpu_sys_main_thread", "used_cpu_user_main_thread",
    "total_commands_processed", "instantaneous_ops_per_sec", "hz", "lazyfree_pending_objects",
    "lazyfreed_objects", "rdb_bgsave_in_progress", "current_cow_size", "current_fork_perc",
    "current_save_keys_processed", "rdb_last_cow_size", "latest_fork_usec",
]

try:
    os.sched_setaffinity(0, {int(HARNESS_CPU)})
except Exception:
    pass


def binary(build):
    return BIN / build / "redis-server"


def environment_info():
    def sh(c):
        try:
            return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
        except Exception:
            return ""
    return {
        "kernel": platform.release(),
        "os": sh("grep PRETTY_NAME /etc/os-release | cut -d= -f2"),
        "cpu": sh("lscpu | grep 'Model name' | cut -d: -f2").strip(),
        "nproc": os.cpu_count(),
        "mem_total_kb": sh("grep MemTotal /proc/meminfo | awk '{print $2}'"),
        "swap_total_kb": sh("grep SwapTotal /proc/meminfo | awk '{print $2}'"),
        "loadavg": sh("cat /proc/loadavg"),
        "python": platform.python_version(),
    }


class Server:
    """A fresh redis-server process with the research base config + overrides."""

    def __init__(self, build, overrides=None, port=PORT, workdir=None):
        self.build = build
        self.port = port
        self.overrides = dict(overrides or {})
        self.workdir = Path(workdir or f"/tmp/redis-exp-{port}")
        self.proc = None

    def start(self):
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.workdir.mkdir(parents=True)
        args = ["taskset", "-c", REDIS_CPU, str(binary(self.build)), str(BASE_CONF),
                "--port", str(self.port), "--dir", str(self.workdir)]
        for k, v in self.overrides.items():
            args += [f"--{k}", str(v)]
        self.log = open(self.workdir / "redis.log", "w")
        self.proc = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT)
        self.r = redis.Redis(port=self.port, decode_responses=True)
        for _ in range(200):
            if self.proc.poll() is not None:
                raise RuntimeError("redis exited; see " + str(self.workdir / "redis.log"))
            try:
                if self.r.ping():
                    break
            except redis.ConnectionError:
                time.sleep(0.02)
        else:
            raise RuntimeError("redis did not start; see " + str(self.workdir / "redis.log"))
        assert self.r.dbsize() == 0
        return self

    @property
    def pid(self):
        return self.proc.pid

    def config(self):
        return self.r.config_get("*")

    def version(self):
        i = self.r.info("server")
        return {"redis_version": i["redis_version"], "redis_git_sha1": i.get("redis_git_sha1"),
                "build": self.build, "commit": (BIN / self.build / "COMMIT").read_text().strip()}

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)  # no persistence: fastest clean stop
            self.proc.wait()
        self.log.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


LOAD_LUA = """
local val = ARGV[1]
local base = tonumber(ARGV[2])
local prefix = ARGV[3]
for i = 4, #ARGV do
  local e = ARGV[i]
  local k = prefix .. (base + i - 4)
  if e == '0' then redis.call('SET', k, val) else redis.call('SET', k, val, 'PXAT', e) end
end
return #ARGV - 3
"""


LOAD_HASH_LUA = """
local val = ARGV[1]
local base = tonumber(ARGV[2])
local prefix = ARGV[3]
local nf = tonumber(ARGV[4])
local args = {}
for f = 1, nf do args[2*f-1] = 'f' .. f; args[2*f] = val end
for i = 5, #ARGV do
  local k = prefix .. (base + i - 5)
  redis.call('HSET', k, unpack(args))
  if ARGV[i] ~= '0' then redis.call('PEXPIREAT', k, ARGV[i]) end
end
return #ARGV - 4
"""


def load_hashes(r, expire_at_ms, fields, value_size, prefix="k:", batch=200):
    """Create hash keys with `fields` fields each (large composite values)."""
    sha = r.script_load(LOAD_HASH_LUA)
    val = "x" * value_size
    exp = np.asarray(expire_at_ms, dtype=np.int64)
    t0 = time.monotonic()
    for off in range(0, len(exp), batch):
        r.evalsha(sha, 0, val, off, prefix, fields, *[str(int(x)) for x in exp[off:off + batch]])
    return time.monotonic() - t0


def load_keys(r, expire_at_ms, value_size, prefix="k:", start_index=0, batch=20000):
    """Create len(expire_at_ms) string keys server-side. expire_at_ms[i]==0 -> no TTL."""
    sha = r.script_load(LOAD_LUA)
    val = "x" * value_size
    exp = np.asarray(expire_at_ms, dtype=np.int64)
    t0 = time.monotonic()
    for off in range(0, len(exp), batch):
        chunk = exp[off:off + batch]
        r.evalsha(sha, 0, val, start_index + off, prefix, *[str(int(x)) for x in chunk])
    return time.monotonic() - t0


def proc_stats(pid):
    """utime/stime (ticks), RSS and context switches straight from /proc (no server I/O)."""
    out = {}
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        out["proc_utime_ticks"] = int(parts[11])
        out["proc_stime_ticks"] = int(parts[12])
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["proc_rss_kb"] = int(line.split()[1])
                elif line.startswith("voluntary_ctxt_switches"):
                    out["proc_vcsw"] = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches"):
                    out["proc_nvcsw"] = int(line.split()[1])
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            kids = f.read().split()
        rss = 0
        for k in kids:  # fork children (BGSAVE): their RSS shows copy-on-write cost
            try:
                with open(f"/proc/{k}/smaps_rollup") as f:
                    for line in f:
                        if line.startswith("Rss:"):
                            rss += int(line.split()[1])
                        elif line.startswith("Private_Dirty:"):
                            out["child_private_dirty_kb"] = out.get("child_private_dirty_kb", 0) + int(line.split()[1])
            except FileNotFoundError:
                pass
        out["child_rss_kb"] = rss
    except FileNotFoundError:
        pass
    return out


def parse_info(raw):
    out = {}
    for line in raw.splitlines():
        if ":" not in line or line.startswith("#"):
            continue
        k, v = line.split(":", 1)
        if k in INFO_FIELDS:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
        elif k.startswith("db") and "keys=" in v:
            d = dict(x.split("=") for x in v.split(","))
            out[f"{k}_keys"] = int(d["keys"])
            out[f"{k}_expires"] = int(d["expires"])
    return out


class Poller(threading.Thread):
    """Sample INFO + /proc at a fixed interval on a dedicated connection."""

    def __init__(self, port, pid, interval, sections=("memory", "stats", "keyspace", "cpu", "persistence")):
        super().__init__(daemon=True)
        self.r = redis.Redis(port=port, decode_responses=True)
        self.pid, self.interval, self.sections = pid, interval, sections
        self.samples, self._stop = [], threading.Event()

    def sample(self):
        t_wall, t_mono = time.time(), time.monotonic()
        raw = self.r.execute_command("INFO", *self.sections)
        if isinstance(raw, dict):  # some client versions pre-parse INFO
            raw = "\n".join(f"{k}:{v}" for k, v in raw.items())
        s = parse_info(raw)
        s["dbsize"] = self.r.dbsize()
        s.update(proc_stats(self.pid))
        s["t_wall"], s["t_mono"], s["poll_rtt"] = t_wall, t_mono, time.monotonic() - t_mono
        self.samples.append(s)
        return s

    def run(self):
        nxt = time.monotonic()
        while not self._stop.is_set():
            self.sample()
            nxt += self.interval
            d = nxt - time.monotonic()
            if d > 0:
                self._stop.wait(d)
            else:
                nxt = time.monotonic()

    def stop(self):
        self._stop.set()
        self.join()


def wait_until_wall(t_wall):
    while True:
        d = t_wall - time.time()
        if d <= 0:
            return
        time.sleep(min(d, 0.05))


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def check_no_swap():
    with open("/proc/meminfo") as f:
        m = {l.split(":")[0]: int(l.split()[1]) for l in f}
    return {"swap_used_kb": m["SwapTotal"] - m["SwapFree"], "mem_available_kb": m["MemAvailable"]}

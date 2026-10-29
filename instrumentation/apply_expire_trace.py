#!/usr/bin/env python3
"""Apply research-only instrumentation to a Redis source tree (7.2.x or 8.x).

Adds a per-cycle trace of activeExpireCycle() into an mmap()-ed buffer
(NOT zmalloc, so it is excluded from used_memory; it IS included in RSS, which
is why instrumented results are never mixed with unmodified results).

Each record: wall-clock start (us), elapsed (us), cycle type (0=slow,1=fast),
timelimit_exit, iterations, keys sampled, keys expired, and the number of keys
still in the expires dict(s) when the cycle ended. Fast cycles that were
*rejected* at the gate are counted (not recorded individually).

New DEBUG subcommands (requires enable-debug-command):
    DEBUG EXPIRE-TRACE-START <capacity>
    DEBUG EXPIRE-TRACE-DUMP <path>       -> writes CSV, returns #records
    DEBUG EXPIRE-TRACE-STOP

Usage: apply_expire_trace.py <redis-src-dir>
"""
import re
import sys
from pathlib import Path

src = Path(sys.argv[1]) / "src"
expire = (src / "expire.c").read_text()
debug = (src / "debug.c").read_text()

if "EXPIRE_TRACE_RESEARCH" in expire:
    print("already patched")
    sys.exit(0)

is8 = "kvstoreSize(db->expires)" in expire
size_expr = "kvstoreSize(server.db[i].expires)" if is8 else "dictSize(server.db[i].expires)"

header = r'''
/* ===== EXPIRE_TRACE_RESEARCH (research-only instrumentation) ===== */
#include <stdio.h>
#include <sys/mman.h>
typedef struct {
    long long start_us;
    long long elapsed_us;
    int type;
    int tl_exit;
    int iterations;
    long sampled;
    long expired;
    unsigned long long expires_left;
} expireTraceRec;
static expireTraceRec *et_buf = NULL;
static size_t et_len = 0, et_cap = 0;
static unsigned long long et_fast_gate_rejects = 0, et_fast_interval_rejects = 0;
void expireTraceStop(void);
void expireTraceStart(size_t cap) {
    expireTraceStop();
    void *p = mmap(NULL, sizeof(expireTraceRec) * cap, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    et_buf = (p == MAP_FAILED) ? NULL : p;
    et_cap = et_buf ? cap : 0; et_len = 0;
    et_fast_gate_rejects = et_fast_interval_rejects = 0;
}
void expireTraceStop(void) { if (et_buf) munmap(et_buf, sizeof(expireTraceRec) * et_cap); et_buf = NULL; et_cap = et_len = 0; }
static unsigned long long expireTraceExpiresLeft(void) {
    unsigned long long n = 0;
    for (int i = 0; i < server.dbnum; i++) n += SIZEEXPR;
    return n;
}
static void expireTraceRecord(long long start, long long elapsed, int type, int tl_exit,
                              int iterations, long sampled, long expired) {
    if (!et_buf || et_len >= et_cap) return;
    expireTraceRec *r = &et_buf[et_len++];
    r->start_us = start; r->elapsed_us = elapsed; r->type = type; r->tl_exit = tl_exit;
    r->iterations = iterations; r->sampled = sampled; r->expired = expired;
    r->expires_left = expireTraceExpiresLeft();
}
long long expireTraceDump(const char *path) {
    FILE *fp = fopen(path, "w");
    if (!fp) return -1;
    fprintf(fp, "# fast_gate_rejects=%llu fast_interval_rejects=%llu\n",
            et_fast_gate_rejects, et_fast_interval_rejects);
    fprintf(fp, "start_us,elapsed_us,type,tl_exit,iterations,sampled,expired,expires_left\n");
    for (size_t i = 0; i < et_len; i++) {
        expireTraceRec *r = &et_buf[i];
        fprintf(fp, "%lld,%lld,%d,%d,%d,%ld,%ld,%llu\n", r->start_us, r->elapsed_us, r->type,
                r->tl_exit, r->iterations, r->sampled, r->expired, r->expires_left);
    }
    fclose(fp);
    return (long long)et_len;
}
/* ===== end EXPIRE_TRACE_RESEARCH ===== */
'''.replace("SIZEEXPR", size_expr)

anchor = "void activeExpireCycle(int type) {"
assert expire.count(anchor) == 1
expire = expire.replace(anchor, header + "\n" + anchor)

# Count fast-cycle gate rejections.
gate_old = re.search(r"(if \(!timelimit_exit &&\n\s+server\.stat_expired_stale_perc[^\n]*\n\s+)return;", expire)
assert gate_old, "gate not found"
expire = expire.replace(gate_old.group(0), gate_old.group(1) + "{ et_fast_gate_rejects++; return; }", 1)
int_old = "if (start < last_fast_cycle + (long long)config_cycle_fast_duration*2)\n            return;"
assert int_old in expire
expire = expire.replace(int_old, "if (start < last_fast_cycle + (long long)config_cycle_fast_duration*2)\n            { et_fast_interval_rejects++; return; }")

# Record at end of the cycle (right after the final elapsed computation).
end_old = "    elapsed = ustime()-start;\n    server.stat_expire_cycle_time_used += elapsed;"
assert expire.count(end_old) == 1
expire = expire.replace(end_old, end_old + "\n    expireTraceRecord(start, elapsed, type, timelimit_exit, iteration, total_sampled, total_expired);")
(src / "expire.c").write_text(expire)

dbg_anchor = '} else if (!strcasecmp(c->argv[1]->ptr,"set-active-expire") &&'
assert debug.count(dbg_anchor) == 1
dbg_code = r'''} else if (!strcasecmp(c->argv[1]->ptr,"expire-trace-start") && c->argc == 3) {
        void expireTraceStart(size_t cap);
        expireTraceStart((size_t)atoll(c->argv[2]->ptr));
        addReply(c,shared.ok);
    } else if (!strcasecmp(c->argv[1]->ptr,"expire-trace-stop") && c->argc == 2) {
        void expireTraceStop(void);
        expireTraceStop();
        addReply(c,shared.ok);
    } else if (!strcasecmp(c->argv[1]->ptr,"expire-trace-dump") && c->argc == 3) {
        long long expireTraceDump(const char *path);
        addReplyLongLong(c, expireTraceDump(c->argv[2]->ptr));
    '''
debug = debug.replace(dbg_anchor, dbg_code + dbg_anchor)
(src / "debug.c").write_text(debug)
print("patched", src, "(8.x)" if is8 else "(7.x)")

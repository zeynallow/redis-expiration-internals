# Source code notes: Redis key expiration

All references were read from the actual source trees built for this study:

| Build | Tag | Commit |
|---|---|---|
| 7.2.17 | `7.2.17` | `83937c51afb53e1c69661c8e1533e0e19b896fe1` |
| 8.8.4 | `8.8.4` | `bab5911b2e3114b98b6ae1533c1a15a57ba539a8` |
| 8.10.3 | `8.10.3` | `1a2574ce1551d3de6e0b4a56756c0fdbc6659e4f` |

Line numbers are for **8.10.3** unless stated otherwise. In `expire.c`, 8.8.4 is byte-identical to 8.10.3
except for line 324 (see N4).

---

## N1. Two expiration paths

**Passive (lazy).** `db.c:2999 expireIfNeeded()`. Every key lookup checks `keyIsExpired()`. On a master
it deletes the key synchronously via `deleteExpiredKeyAndPropagate()` (`db.c:2890`) and propagates a
`DEL`/`UNLINK`. On a replica it only *reports* the key as expired (`KEY_EXPIRED`) and does not delete it.
**Consequence:** keys that are never read again are reclaimed only by the active path.

**Active.** `expire.c:287 activeExpireCycle(int type)`, called from two places:

* SLOW cycle: `server.c:1315`, inside `databasesCron()` (`server.c:1310`), which `serverCron` runs
  `hz` times per second.
* FAST cycle: `server.c:2009`, inside `beforeSleep()`, i.e. **once per event-loop iteration**. The more often
  the event loop wakes up (client traffic, monitoring polls), the more often a FAST cycle *may* run.

## N2. Tuning constants and the effort knob

`expire.c:96-99`

```c
#define ACTIVE_EXPIRE_CYCLE_KEYS_PER_LOOP 20      /* Keys for each DB loop. */
#define ACTIVE_EXPIRE_CYCLE_FAST_DURATION 1000    /* Microseconds. */
#define ACTIVE_EXPIRE_CYCLE_SLOW_TIME_PERC 25     /* Max % of CPU to use. */
#define ACTIVE_EXPIRE_CYCLE_ACCEPTABLE_STALE 10   /* % of stale keys after which we do extra efforts. */
```

`expire.c:290-300`. `effort = active-expire-effort - 1` (0..9):

| effort (config) | keys/loop | fast budget (µs) | slow CPU % | acceptable stale % |
|---|---|---|---|---|
| 1 (default) | 20 | 1000 | 25 | 10 |
| 4 | 35 | 1750 | 31 | 7 |
| 7 | 50 | 2500 | 37 | 4 |
| 10 | 65 | 3250 | 43 | 1 |

The slow-cycle budget is `timelimit = slow_time_perc * 1e6 / hz / 100` µs (`expire.c:347`). That is
**25 ms per slow cycle at hz=10**, 2.5 ms at hz=100. The same CPU share is cut into slices 10× shorter.
**Consequence:** `hz`, not effort, sets the length of the longest stall the active cycle causes.

## N3. Sampling is a sequential scan cursor, not random sampling

`expire.c:425-438`

```c
long max_buckets = num*20;
...
while (data.sampled < num && checked_buckets < max_buckets) {
    db->expires_cursor = kvstoreScan(db->expires, db->expires_cursor, -1,
                                     expireScanCallback, expirySamplingShouldSkipDict, &data);
    if (db->expires_cursor == 0) { db_done = 1; break; }
    checked_buckets++;
}
```

The cycle visits whole hash buckets of the `expires` dict in `SCAN` cursor order. `db->expires_cursor`
persists across cycles. The switch from `dictGetRandomKey()` to bucket scanning is commit `ffc7e509a`
("Expire cycle: scan hash table buckets directly", antirez, 2019-11-15, first released in 6.0-rc1).
The current public EXPIRE documentation still says Redis "tests a few keys **at random**"
(`redis/docs` `content/commands/expire.md`, main @ `85cd0a83`).

**Consequence:** all keys hash uniformly, so the fraction of expired keys *ahead of the cursor* stays at
its initial value until the cursor has swept the whole table. The process is a deterministic sweep. It
is not a random sample whose hit-rate decays as garbage is removed.

## N4. The stop rule (the "acceptable stale" threshold)

`expire.c:447`

```c
repeat = db_done ? 0 : (data.sampled == 0 ||
                        (data.expired * 100 / data.sampled) > config_cycle_acceptable_stale);
```

After each batch of about 20 keys, the loop repeats for this DB only if **more than 10%** of the batch was
expired. Otherwise the cycle moves on and the DB gets one more batch in the next cycle. In 7.2.17 this is
the `do { } while (data.sampled == 0 || ... )` condition at `expire.c:307`.

**Consequence (predicted, then measured in F1):** if the expired keys are ≤10% of the keys *that have a
TTL*, each slow cycle examines only about one batch of 20 keys (somewhat more due to random repeats).
Reclamation then proceeds at roughly `hz × 20 × p / (1-P_repeat)` keys/s, a few tens of keys per second,
however many expired keys exist. Persistent (no-TTL) keys are not in `db->expires` and do not count.
Long-TTL keys do count. A small rise in the expired share above 10% switches to the
"repeat until time limit" regime, which reclaims about 150k keys/s.

## N5. FAST-cycle gate and the #15412 unit fix (8.10.0+)

`expire.c:318-330` (8.10.3):

```c
if (type == ACTIVE_EXPIRE_CYCLE_FAST) {
    if (!timelimit_exit &&
        server.stat_expired_stale_perc * 100 < config_cycle_acceptable_stale)
        return;
    if (start < last_fast_cycle + (long long)config_cycle_fast_duration*2)
        return;
    last_fast_cycle = start;
}
```

`stat_expired_stale_perc` is a fraction in [0,1] (an EMA updated at `expire.c:505`). Up to 8.8.4,
including 7.2.17 (`expire.c:178`), the code compared the fraction directly against a percent (10), so
the stale trigger never fired. FAST cycles then ran only when the previous cycle had hit its time limit.
The gate was introduced with the stale threshold in `276680568` (2019, "Expire cycle: tollerate less
stale keys"). Commit `3071bf6d7` (redis/redis#15412, fixing #15410, 2026-07-13) added `* 100`. It first shipped in
8.10.0, and **8.8.4 does not have it**.

**Consequence:** with ≥10% stale keys estimated, 8.10 runs FAST cycles even when the last cycle did not
hit the time cap. The rate limit is one FAST cycle per `2 × fast_duration` (2 ms) and per event-loop
iteration.

## N6. Time-limit check granularity

`expire.c:466-492`. The elapsed time is checked only when `(iteration & 0xf) == 0`, every 16
iterations. Each iteration processes up to `keys_per_loop` keys (20 to 65). The loop over the 16 DBs also
increments `iteration`, even for empty DBs.

**Consequence:** a cycle can overrun its budget by up to 16 × keys_per_loop key deletions. That is
harmless for small strings. For large composite values freed synchronously (`lazyfree-lazy-expire no`,
the default), each deletion is expensive and the 1 ms FAST budget is exceeded (F6).

## N7. Cron cadence stretches under expiry load

`ae.c:335-340`

```c
retval = te->timeProc(eventLoop, id, te->clientData);
...
now = getMonotonicUs();
if (retval != AE_NOMORE) te->when = now + (monotime)retval * 1000;
```

The next `serverCron` is scheduled `1000/hz` ms after the *end* of the current one. When a slow cycle
uses its full 25 ms, the cron period becomes about 125 ms. The effective slow-cycle frequency drops from
10 Hz to about 8 Hz, and the effective CPU share from 25% to about 20%. We measured 126.1 ms median
period during mass expiry vs 100.5 ms idle.

## N8. Sparse-table skip and dict resizing

`expire.c:130-145 expirySamplingShouldSkipDict()` (8.x) and `expire.c:249` (7.2.17): if the expires dict
is <1% full (and larger than the initial size), active expiry skips it: "The dictionary will be resized
asap".

* 7.2.17: dicts shrink **only** in `serverCron → tryResizeHashTables()` (`server.c:604`), and only when
  there is **no child process** (`databasesCron`). During a BGSAVE/AOF rewrite, a mass-expired table
  cannot shrink. Once fill drops under 1%, active expiry pauses until the child exits (tested in F5).
* 8.x: `dict.c:661/982 _dictShrinkIfNeeded()` shrinks on delete. With a child process
  (`DICT_RESIZE_AVOID`) shrinking is still allowed once fill ≤ 1/32 (`dict.c:413-418, 1688-1703`), which
  is above the 1% skip threshold. So the skip should rarely trigger. **This prediction was wrong (see N11, F5, F8).** The skip compares against old + new buckets *during* the shrink, so it does trigger, in 8.x as well.

## N9. What is (and is not) observable through INFO

* `INFO keyspace` `keys=` / `expires=` and `DBSIZE` count **physically present** keys, including
  logically expired ones. Neither triggers passive expiry.
* `expired_stale_perc` is the EMA of the share of sampled keys found expired (×100 in INFO). Because
  sampling follows the scan cursor (N3), it estimates the stale share *of keys with a TTL*. It does not
  estimate the share of all keys or the bytes.
* `expired_time_cap_reached_count`, `expire_cycle_cpu_milliseconds` (cumulative cycle time),
  `expired_keys_active` (8.x).
* No metric reports the **bytes** held by expired-but-present keys.

## N10. Memory reclamation after deletion

Deleting a key frees its allocations through jemalloc. `used_memory` drops immediately. RSS drops only
when jemalloc purges dirty pages: background thread (`jemalloc-bg-thread yes`) plus decay, or
`MEMORY PURGE`. Partly-used runs/slabs stay resident (allocator fragmentation). Measured in F7.

## N11. The 1% skip rule stalls active expiry during the expires-dict shrink (found experimentally)

Discovered in the trace data (finding F5). The pieces:

1. `expirySamplingShouldSkipDict()` (`expire.c:130-145`) skips a dict when
   `numkeys * 100 / dictBuckets(d) < 1`.
2. `dictBuckets(d)` is `DICTHT_SIZE(ht[0]) + DICTHT_SIZE(ht[1])` (`dict.h:229`). While a resize is in
   progress it counts **both** tables.
3. Mass deletion triggers a shrink. 8.x: `_dictShrinkIfNeeded()` on delete (`dict.c:661/982`) once
   fill ≤ 1/8. 7.2.17: `tryResizeHashTables()` in cron (`server.c:604`) once fill < 10%. For a
   1M-key expires dict (2^20 buckets) the new table is 2^17. During the rehash
   `dictBuckets = 2^20 + 2^17 = 1,179,648`, so the dict is skipped once fewer than
   **11,796** keys remain.
4. Nothing on the expire path advances that rehash. `kvstoreScan`/`dictScan` pause rehashing, and once
   the dict is skipped there are no deletes and therefore no `_dictRehashStep()`. Progress comes only
   from `databasesCron()` → `kvstoreIncrementallyRehash()` (`server.c:1350-1360`, `kvstore.c:674`). It
   has a **shared 1 ms budget per cron tick** (`INCREMENTAL_REHASHING_THRESHOLD_US`, `server.h:147`)
   and serves `db->keys` **before** `db->expires`. The main keyspace dict is usually shrinking too after
   a mass expiry, so its rehash goes first.
5. Cron rehashing is disabled entirely while a child process exists (`server.c:1331`,
   `if (!hasActiveChildProcess())`) and when `activerehashing no`.

**Predicted consequences** (tested in suites `latency_trace`, `rehash_stall`, `fork`):
* a plateau of just under 1% of the original key count, lasting until the cron has finished rehashing
  both tables. That time grows with keyspace size.
* with `activerehashing no`, or during a BGSAVE/AOF rewrite, the plateau lasts until other dict
  operations (writes with TTL to the same expires dict) finish the rehash, or until the child exits.

The comment above the rule says "The dictionary will be resized asap". That assumption breaks when the
resize is itself what keeps the fill ratio below 1%.

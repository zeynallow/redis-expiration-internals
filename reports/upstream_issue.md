# Upstream issue: filed as [redis/redis#16111](https://github.com/redis/redis/issues/16111)

**Title:** Active expiry pauses with ~1% of keys still expired while the `expires` dict is being shrunk (`expirySamplingShouldSkipDict` counts both rehash tables)

## Summary

After a mass expiration, `activeExpireCycle()` stops examining a database's `expires` dict while
just under 1% of the original key count is still expired-but-present. It resumes only after the
cron's incremental rehashing has finished the shrink that the mass deletion started. The pause:

* happens on every mass expiry we measured (7.2.17, 8.8.4, 8.10.3, and unchanged on `unstable` @ `a76a3689`),
* grows with keyspace size (≈0.4 s at 1M keys, ≈6 s at 8M keys, idle server, default config),
* lasts indefinitely with `activerehashing no` (≥55 s in our runs with no other traffic, i.e. until
  unrelated dict operations finish the rehash), and
* under client load is about a third of the total time needed to reclaim 1M simultaneously-expired keys.

The threshold is exact. The pause starts when the remaining key count falls below
`1% × (2^k + 2^(k-3))` (old table + new table), e.g. 11,796 for 1M keys. Observed plateaus:
11,772–11,796 (1M), 23,576–23,591 (2M), 47,168–47,184 (4M), 94,358–94,369 (8M).

## Mechanism (8.10.3 line numbers)

1. `expirySamplingShouldSkipDict()` (`src/expire.c:130-145`) skips a dict when
   `numkeys * 100 / dictBuckets(d) < 1`, with the comment "The dictionary will be resized asap".
2. `dictBuckets()` (`src/dict.h:229`) is `size(ht[0]) + size(ht[1])`. While a shrink is in
   progress it counts the old, large table **and** the new one.
3. The mass deletion itself triggers the shrink (`_dictShrinkIfNeeded()` on delete in 8.x,
   `tryResizeHashTables()` from cron in 7.2). For 2^20 → 2^17 buckets, `dictBuckets` is 1,179,648
   during the rehash, so the dict is skipped once fewer than 11,796 keys remain.
4. Nothing on the expire path advances that rehash. `dictScan` pauses rehashing, and once the dict
   is skipped there are no more deletes and therefore no `_dictRehashStep()`. Progress comes only
   from `databasesCron()` → `kvstoreIncrementallyRehash()`, which gets a shared 1 ms per cron tick
   and handles `db->keys` before `db->expires`. That cron path is disabled entirely with
   `activerehashing no` and while a child process exists (`server.c:1331`).

So the skip waits for a resize that, at that point, is the very thing keeping the fill under 1%.

## Reproduction (no instrumentation needed)

```
redis-server --save "" --appendonly no
# load 1,000,000 keys that all expire at the same absolute time T, e.g. with Lua:
#   for i=1,N do redis.call('SET','k:'..i,'x','PXAT',T) end
# after T, poll once per 100 ms:
redis-cli DBSIZE
```

`DBSIZE` drops at ~150k keys/s, then stays at ~11,7xx for 0.3–1 s (or forever with
`--activerehashing no`), then drops to 0 in one or two cycles. Our harness and raw data:
`benchmarks/expiration_test.py`, suite `rehash_stall` in `benchmarks/run_all.py`.

## Candidate fix

Do not apply the sparse-table skip while the dict is already rehashing:

```diff
-    if (buckets > DICT_HT_INITIAL_SIZE && (numkeys * 100/buckets < 1)) {
+    if (buckets > DICT_HT_INITIAL_SIZE && (numkeys * 100/buckets < 1) && !dictIsRehashing(d)) {
```

Rationale: the skip exists because sampling a sparse table is expensive. A table being shrunk is
"sparse" only because both tables are counted. The scan cost per call is still bounded by
`max_buckets = num*20` and by the cycle's time limit. Alternatives considered: computing the fill
against the destination table only, or running a bounded `dictRehashMicroseconds()` on the skipped
dict.

Evaluation (8.10.3 vs 8.10.3 + patch, both with our research-only trace, 3 reps each):

| condition | stock 8.10.3 | 8.10.3 + patch |
|---|---|---|
| 1M keys, `activerehashing no`, idle | **11,792 keys never reclaimed** (stall ≥54 s, end of window) | all reclaimed, 6.74 s [6.72–7.70] |
| 1M keys, 10k GET/s load | 3.84 s [3.62–4.26] incl. 1.27 s stall | **2.51 s** [2.29–2.62], no stall |
| 4M keys, idle | 30.7 s [30.4–31.8] incl. 3.0 s stall | 28.1 s [26.7–29.4], no stall |
| 1M keys, idle | 6.35 s [5.81–6.53] incl. 0.41 s stall | 6.97 s [6.37–7.66], no stall |
| longest single cycle | 25.6–27.6 ms | 25.5–26.5 ms |

Medians [min–max]. Times are from a per-cycle trace (time of the cycle that removed the last key),
not from client latency. The patch removes the stall in every condition and does not lengthen
individual cycles. A first, block-ordered run suggested 18% more expire CPU for the patch at 1M keys idle. A
round-robin re-test (6 rounds, stock and patched alternating, ratios paired per round) shows no
measurable cost: 1.71 vs 1.72 µs per expired key, paired ratio 1.02 [0.89–1.16]. Time to remove 1M
keys at idle: 8.02 s vs 8.49 s. Maintainers may prefer an alternative that keeps the skip but advances the rehash.

## Severity

Low to moderate. With default settings it is a bounded delay that scales with keyspace size, and
the memory involved is ≈1% of the expired keys. It becomes unbounded with `activerehashing no`, and
on every tested version (7.2.17, 8.8.4, 8.10.3) it lasts as long as a BGSAVE/fork child is alive
(≥95 s in our runs; 7.2 never starts the shrink while a child exists, 8.x starts it but cron
rehashing is disabled). The fix is one condition; its effect under a fork was not tested.

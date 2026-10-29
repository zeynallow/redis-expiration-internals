# Findings

Every number below comes from `results/processed/*.csv`, which `analysis/analyze.py` regenerates from
`results/raw/`. "Median [min–max]" across repetitions unless stated otherwise. Builds marked `-trace`
carry the research-only per-cycle trace (instrumentation/). Latency numbers come only from unmodified
builds. Source references are explained in `source_code_notes.md` (N1–N11).

Status tags: **[expected]** behaviour that follows directly from the documented or obvious design;
**[undocumented]** follows from the code but is not described in the docs and is surprising in
magnitude; **[defect?]** looks like an unintended interaction worth raising upstream.

---

## F1. The 10% rule is a cliff, and it sits much higher than 10% [undocumented]

*Suites `cliff` (v185, 3 reps per point), `cliff_epoch_v180` (24 runs, v180), `cliff_versions`
(v180), `sweep`, `persist`, `bytes`. 1M keys with a TTL. A share p of them expires at T, the rest
have a 24 h TTL. Idle server, default config (effort 1, hz 10). Rate = keys reclaimed per second
between T+10 s and T+60 s.*

| expiring share | model rate (keys/s) | 8.8.4 rate | 8.10.3 rate | 8.8.4 removed after 60 s | 8.10.3 removed after 60 s | 8.8.4 all removed after | 8.10.3 all removed after |
|---|---|---|---|---|---|---|---|
| 5% | 11 | 11 | 11 | 1.3% | 1.3% | > 60 s | > 60 s |
| 8% | 20 | 21 | 22 | 1.6% | 1.6% | > 60 s | > 60 s |
| 10% | 30 | 31 | 31 | 1.8% | 1.9% | > 60 s | > 60 s |
| 11% | 35 | 37 | 43 | 2.0% | 2.3% | > 60 s | > 60 s |
| 12% | 43 | 45 | 61 | 2.2% | 2.9% | > 60 s | > 60 s |
| 15% | 74 | 77 | 168 | 3.1% | 6.4% | > 60 s | > 60 s |
| 20% | 194 | 209 | 441 | 6.3% | 12.7% | > 60 s | > 60 s |
| 25% | 548 | 606 | 1307 | 14.6% | 31.0% | > 60 s | > 60 s |
| 30% | 1691 | 1932 | 3588 | 38.5% | 72.2% | > 60 s | > 60 s |
| 35% | 5777 | 6385 | 10037 | 100.0% | 100.0% | 54.4 s | 35.8 s |
| 40% | 22152 | 19366 | 26208 | 100.0% | 100.0% | 16.5 s | 14.8 s |
| 50% | (time-limited) | – | – | 100.0% | 100.0% | 5.3 s | 5.4 s |

Model column: slow cycles only, at 10/s, in batches of 20 keys, with a batch repeated when more than
10% of it (≥3 of 20) was expired: `rate = 10 × 20/(1−q(p)) × p` with `q(p) = P[Bin(20,p) ≥ 3]` (N3, N4).

* **Below ~12%, active expiry nearly stops.** Each 20-key batch rarely has ≥3 expired keys, so a
  database gets about one batch per cron tick. With 10% expired that is ~31 keys/s, and one pass of
  the cursor over a 1M-key table takes about an hour (measured sampling ≈ 300–330 keys/s).
  1.3–2.9% of the expired keys are removed in the first minute. The server stays idle meanwhile:
  expire CPU in that minute is ~30–36 ms (`cliff_versions`).
* **The real cliff is at 30–35%, not 10%.** Run lengths are geometric with mean 1/(1−q(p)), so the
  rate rises smoothly until q→1 and the cycle runs into its time limit. Only from ~35% does a full
  sweep finish within a minute.
* **The model explains 8.8.4 almost exactly** (within 0.9–1.15× from 5% to 40%; `charts/cliff.png`).
  8.10.3 matches it up to 11%, then sits 2–2.3× above it once its stale estimate passes 10% and the
  repaired FAST trigger (#15412) adds about one FAST cycle per event-loop wake (F2, F4).
* **Same behaviour on all versions and both machines.** At 10% expiring with defaults: 7.2.17 1.8%,
  8.8.4 1.9%, 8.10.3 1.9% removed after 60 s (`cliff_versions`, v180). The v180 and v185 runs of the
  low-p points agree within 3–7% (e.g. 10%: 32.3 vs 31.2 keys/s). This regime is set by cycle
  counts, not CPU speed.
* **Mitigations at 10% expiring** (`cliff_versions`, v180, 3 reps): `hz 100` removes ~18% in 60 s
  (10× more cycles), at 270–360 ms of expire CPU per minute. `active-expire-effort 10` (threshold 1%,
  65-key batches) removes 100% in 8.4 s (8.10.3) / 12.9–13.1 s (7.2.17, 8.8.4), but its longest cycle
  grows from ~0.5 ms to ~20 ms.
* **Persistent keys do not cause this; long-TTL keys do.** `persist` (v180, 3 reps): when the
  other 900k keys have *no* TTL, the 100k expiring keys are gone in 0.95 s (8.10.3), 1.10 s (8.8.4),
  1.25 s (7.2.17). When those 900k keys have a 24 h TTL instead (`cliff`, p = 10%), only 1.8–1.9%
  of the expired keys are gone after 60 s. Only `db->expires` is sampled, so the denominator is
  "keys with a TTL", not "keys".
* **Count, not bytes.** `bytes` (v180, 7.2.17): 1M live 64-byte keys + 100k expired 4 KB keys. After
  120 s, 96.7–96.9k of the 100k expired keys are still present and `used_memory` is ~665 MiB, against
  ~150 MiB for the live data. `expired_stale_perc` reads ~8%, which looks healthy, while >75% of
  `used_memory` is expired data.
* **The sweep finishes in exactly one cursor pass.** `sweep` (v185, 2 reps): 100k TTL keys, 10%
  expire. Half are gone at 163 s, all at 326 s, linearly. Prediction: 100k keys ÷ ~310 sampled
  keys/s = ~320 s.
* **A client-side SCAN clears it in seconds.** `mitigation` (v185, 3 reps): at 10% expiring among
  1M TTL keys, one full `SCAN` started 5 s after T removed all ~100k stale keys. 8.10.3 took
  1.06–1.14 s (COUNT 1000, p50 1.0 ms/call) or 2.40–2.56 s (COUNT 100, p50 0.23 ms/call). 7.2.17 took
  1.50–1.95 s or 2.98–3.08 s. On a master, SCAN calls `expireIfNeeded()` on every key it visits
  (`db.c` `scanCallback`, 8.10.3 line ~1720).
* The docs say active expiry "tests a few keys at random". Random sampling would not produce this
  sweep with its sharp threshold (F10).


## F2. Under steady churn, ≤8.8.x holds ~21% of TTL keys as garbage and oscillates; 8.10 holds ~9% [undocumented; changed by #15412]

*Suites `churn` (v180), `churn_revert` and `churn_long` (v185). The load generator writes 5,000
`SET key val PX 60000` per second (8 connections, open loop), i.e. 300k live short-TTL keys and
5,000 expirations/s at equilibrium. The live count is computed exactly from the generator's
per-request log, so stale = DBSIZE − live. Steady state = second half of each run. 2 reps each.*

Without long-TTL keys (240 s runs, `churn_revert`, all on v185):

| build | stale keys (median) | swing in steady state | stale share of TTL keys | FAST cycles/s | expire CPU |
|---|---|---|---|---|---|
| 8.8.4 | 81,941 | 51,788–104,805 | 21.5% | 0 | 1.6% |
| 8.10.3 with #15412 reverted | 80,719 | 55,461–101,649 | 21.2% | 0 | 1.6% |
| **8.10.3** | **30,453** | 29,612–31,506 | **9.2%** | 440 | 3.7% |

With 1M additional keys that carry a 24 h TTL (720 s runs, `churn_long`, v185):

| build | stale keys (median) | swing in steady state | peak | stale share | used_memory | expire CPU |
|---|---|---|---|---|---|---|
| 8.8.4 | 345,549 | 227,240–422,340 | 561,345 | 21.0% | 240 MiB | 2.0% |
| 8.10.3 with #15412 reverted | 343,571 | 230,876–435,502 | 553,024 | 20.9% | 239 MiB | 2.0% |
| **8.10.3** | **132,356** | 130,564–137,218 | 226,139 | **9.2%** | 211 MiB | 4.3% |

(Chart: `charts/churn_long.png`.) What this shows:

1. **The garbage is set by a share, not a count.** At equilibrium the stale share of *keys with a TTL*
   is roughly constant (≈21% pre-fix, ≈9% in 8.10), so the absolute garbage scales with every key
   that has a TTL. Adding 1M long-TTL keys that never expire during the test multiplies the expired
   keys held in memory by 4.2–4.3× (82k → 346k on 8.8.4; 30k → 132k on 8.10.3). The 1M extra keys
   dilute the stale share, the sampler stops repeating (F1), and garbage piles up until the share
   is high enough again.
2. **Pre-fix builds oscillate.** Stale keys grow almost unreclaimed at the full 5,000/s until the
   share reaches ~30% (peak 553–567k), a fast sweep then removes a large part, and the cycle repeats
   with a ~150 s period and decaying amplitude. This is a relaxation oscillation produced by the
   sharp threshold in F1.
3. **#15412 is the cause.** Reverting that one line in 8.10.3 reproduces 8.8.4 to within ~1%,
   including the oscillation (the two curves overlap in the chart). With the fix, the EMA stale
   estimate (`expired_stale_perc` ≈ 14%) keeps the FAST gate open. Every event-loop iteration then
   adds a FAST cycle (≈430–440/s under this load), holding the stale share near the 10% target. The
   cost is about 2 percentage points more expire CPU.
4. The v180 `churn` suite (240 s) agrees for 7.2.17 and 8.8.4 (≈79–80k stale, 21%; no FAST cycles)
   and for 8.10.3 (≈31k, 9.4%). 7.2.17 with `active-expire-effort 10` holds ≈69k (5% of TTL keys)
   at 2.4% CPU.

## F3. Mass expiry latency is set by `hz`, not by `active-expire-effort` [expected mechanism, magnitude worth knowing]

*Suite `latency` (unmodified builds, 10k GET/s open loop over 8 connections, 1M keys expiring at once, 5 reps).*

| version | effort | hz | p99 before | p99 during expiry | max during | expiry done after |
|---|---|---|---|---|---|---|
| 7.2.17 | 1 | 10 | 1.7 ms | **27.1 ms** | 31.3 ms | 3.5 s |
| 8.8.4 | 1 | 10 | 1.4 ms | **26.3 ms** | 29.6 ms | 3.5 s |
| 8.10.3 | 1 | 10 | 1.3 ms | **26.7 ms** | 30.1 ms | 3.5 s |
| 7.2.17 | 10 | 10 | 1.5 ms | **49.8 ms** | 53.9 ms | 3.0 s |
| 8.8.4 | 10 | 10 | 1.4 ms | **49.9 ms** | 57.3 ms | 3.5 s |
| 8.10.3 | 10 | 10 | 1.1 ms | **50.4 ms** | 56.5 ms | 3.5 s |
| 7.2.17 | 1 | 100 | 0.7 ms | **5.4 ms** | 9.4 ms | 2.0 s |
| 8.8.4 | 1 | 100 | 1.1 ms | **8.1 ms** | 14.9 ms | 2.0 s |
| 8.10.3 | 1 | 100 | 0.9 ms | **8.5 ms** | 14.6 ms | 2.0 s |
| (no expiry) | 1 | 10 | 0.5–1.0 ms | 0.5–0.9 ms | 5.5–7.2 ms | – |

("expiry done" has 0.5 s resolution, from DBSIZE polling. Chart: `charts/latency_scatter.png`.)

* The p99 during expiry equals the slow-cycle time slice: `25% × 1/hz` = 25 ms at hz=10. Effort 10
  raises it to `43% × 100 ms` = 43 ms, which shows up as ~50 ms. hz=100 shrinks it to 2.5 ms per slice
  (N2).
* **Effort 10 doubles tail latency and does not finish faster under load** (3.0–3.5 s vs 3.5 s).
  hz=100 finishes *faster* (2.0 s) with a 3–5× lower tail. The price is a higher median (p50 rises from
  ~0.2 ms to ~1 ms), because the event loop is interrupted 100 times/s.
* On an idle server effort does speed things up (suite `effort`, 1M keys; block-ordered, but the
  direction is confirmed by the round-robin `interleave` suite, F9): 8.10.3 7.48 s [6.99–7.71]
  at effort 1 vs 4.84 s at effort 10, with the longest cycle going from 26.9 ms to 46.8 ms. Total expire
  CPU barely changes (1.5 s ≈ 1.5 µs/key). Effort only compresses the same work into longer slices.

## F4. Client traffic makes Redis expire keys about 2× faster, by running FAST cycles in beforeSleep [undocumented]

*Suites `latency_trace` (trace builds, same load as F3), `effort` (idle), `observer`.*

| | idle (effort suite) | 10k req/s (latency_trace) |
|---|---|---|
| time to remove 1M keys (8.10.3) | 7.48 s | 3.06–3.12 s |
| FAST cycles run | ~124 | ~700 (≈230/s) |
| share of deletions done by FAST cycles | small | **67–69%** |
| expire duty cycle | ~20% | ~40% |

Every event-loop iteration calls `beforeSleep()`, which may start a 1 ms FAST cycle (N1, N5). An
idle server wakes only for `serverCron` (10/s). A busy one wakes thousands of times per second, and
FAST cycles are rate-limited only to one per 2 ms. Simultaneous expiry looks the same in all four
builds (7.2.17, 8.8.4, 8.10.3, 8.10.3-revert15412): the slow cycle hits its time cap, and that alone
opens the FAST gate. Monitoring traffic counts too. In the `observer` suite (v180, idle,
1M keys, 3 reps), polling `INFO` at 100 Hz cut the reclamation time of 8.10.3 from 7.65 s [7.48–7.94]
to 5.24 s [5.03–5.27], because FAST cycles rose from 74 to 722 (8.8.4: 7.99 s → 5.84 s). Polling at
5 Hz (this study's default) and 1 Hz gave the same result (7.63 s vs 7.65 s), so the measurements here
are not perturbed by the poller.

## F5. Active expiry stops at ~1% remaining while the expires table shrinks [defect?]

*Found in the `latency_trace` and `effort` traces, characterised in `rehash_stall`.*

Every 1M-key mass expiry, on every version, idle or loaded, has a window in which keys remain in
`db->expires` but each cycle samples **zero** keys:

* plateau at **11,726–11,796** keys remaining in all 75 idle runs of the `effort` suite (3 versions,
  5 efforts), and 11,122–11,795 under load. The predicted ceiling is `1% × (2^20 + 2^17) = 11,796`.
* length 0.41–1.06 s idle, 0.82–1.13 s under load. Under load that is about
  **a third of the total reclamation time** (3.0–3.4 s).
* 7.2.17 shows a second, smaller plateau at ~1,280–1,310 keys, the next shrink level.

Mechanism (N11): `expirySamplingShouldSkipDict()` skips a dict whose fill is below 1%. Fill is computed
against `dictBuckets()` = old table + new table **while a shrink is in progress**. The shrink itself
pushes fill under 1%, and nothing on the expire path advances it. Cron rehashing (1 ms per tick, main
keyspace first) eventually finishes and expiry resumes.

Scaling and persistence (`rehash_stall`, v185, idle, 2 reps each). The plateau value matches the
prediction `1% × (2^k + 2^(k−3))` to within 2–17 keys at every size:

| keys expiring | predicted ceiling | plateau (7.2.17) | plateau (8.10.3) | stall length 7.2.17 | stall length 8.10.3 |
|---|---|---|---|---|---|
| 1M | 11,796 | 11,772–11,788 | 11,794–11,796 | 0.31–0.52 s | 0.42–0.53 s |
| 2M | 23,592 | 23,576–23,591 | 23,589 | 1.02–1.44 s | 0.92 s |
| 4M | 47,185 | 47,168–47,171 | 47,177–47,184 | 2.36–3.08 s | 2.25–2.86 s |
| 8M | 94,371 | 94,358–94,368 | 94,364–94,369 | 4.91–6.06 s | 5.49–6.41 s |
| 1M, `activerehashing no` | 11,796 | 11,784–11,793, **never reclaimed** (≥55 s) | 11,794, **never reclaimed** (≥55 s) | – | – |

**Candidate fix** (`instrumentation/fix_skip_rehashing.patch`: do not skip while `dictIsRehashing(d)`),
suite `fixskip`, v185, 3 reps, trace builds:

| condition | stock 8.10.3 | + patch |
|---|---|---|
| 1M, `activerehashing no` | 11,792 keys never reclaimed | all reclaimed, 6.74 s |
| 1M under 10k GET/s | 3.84 s (1.27 s stall) | **2.51 s** (no stall) |
| 4M idle | 30.7 s (3.0 s stall) | 28.1 s |
| 1M idle (block-ordered) | 6.35 s [5.81–6.53] (0.41 s stall) | 6.97 s [6.37–7.66] |
| 1M idle (`interleave`, round-robin, 6 rounds) | 8.49 s [7.81–9.52] | 8.02 s [7.79–8.68] |

The patch removes the stall everywhere and does not lengthen individual cycles (max 25.5–26.5 ms).
The block-ordered `fixskip` suite showed 18% more expire CPU for the patch at 1M idle. The
round-robin `interleave` suite shows no cost: paired ratio 1.02 [0.89–1.16], time 8.02 s vs 8.49 s
(see F9). The earlier difference was host drift. Client latency in `fixskip` is not reported: the v185 host had sporadic ~150 ms
VM pauses that made client-side numbers unreliable (see methodology).

Status: still present on `unstable` @ `a76a3689` (2026-10-28). A web search found no prior report
of this interaction.
Reported upstream as [redis/redis#16111](https://github.com/redis/redis/issues/16111); write-up: `reports/upstream_issue.md`.

## F6. Large values break the time budget; `lazyfree-lazy-expire` fixes it [expected mechanism, undocumented magnitude]

*Suite `bigvalues` (v185, trace builds, 3 reps): 20k hashes × 1,000 fields (hashtable encoding;
1,000 > the default `hash-max-listpack-entries 512`) expire at the same ms. Idle server.*

| version | lazyfree-lazy-expire | time to remove all | longest slow cycle (budget 25 ms) | FAST cycles over 1 ms budget | longest FAST cycle |
|---|---|---|---|---|---|
| 7.2.17 | no (default) | 7.6 s | **60–77 ms** | 100% | 4.1–7.7 ms |
| 8.10.3 | no (default) | 4.9–5.1 s | **49–61 ms** | 100% | 2.7–**48.6** ms |
| 7.2.17 | yes | 0.52–0.73 s | 25–29 ms | 67–100% | 1.3–5.6 ms |
| 8.10.3 | yes | 0.38–0.54 s | 25.3–25.5 ms | 33–67% | 1.6–5.0 ms |

* The clock is read only every 16 iterations (N6). With synchronous freeing a 1,000-field hash costs
  tens of µs, so 16 iterations × 20 keys add up to tens of ms before the first check. Slow cycles
  run 2–3× over budget, and FAST cycles routinely exceed theirs: one 8.10.3 FAST cycle ran 48.6 ms
  against a 1 ms budget. Each of those is a stall of the event loop.
* With `lazyfree-lazy-expire yes` (default **no** in all three versions), large values are handed to
  the background thread (`lazyfreed_objects` = 20,000). Cycles stay near budget and the keys
  disappear 10–15× sooner. The memory itself is released afterwards, off the main thread.
* An earlier version of this experiment used 500-field hashes. Those are listpack-encoded (one
  allocation) and showed none of this. It was discarded as a design error
  (`results/raw/_discarded/bigvalues_listpack_design_error/`).

## F7. Reclamation time scales linearly; used_memory is honest, RSS lags for small values [expected]

*Suite `core` (v185, unmodified builds, idle, simultaneous expiry, versions interleaved per round).
Stopped early at the author's request: 61 of 108 planned runs. Round 0 covers every cell except
7.2.17 × 1M × 4 KB, and round 1 covers most cells (n = 1–2 per cell, see `core_summary.csv`).*

| keys × value | time to remove all (7.2.17 / 8.8.4 / 8.10.3) | used_memory loaded → after | RSS loaded → 12 s after → after `MEMORY PURGE` (8.10.3) |
|---|---|---|---|
| 10k × 64 B | 0.4 / 0.4 / 0.3 s | 3 MiB → 1.4 MiB | 11 → 10 → 10 MiB |
| 100k × 64 B | 1.1 / 1.0 / 0.7 s | 17 MiB → 1.7 MiB | 27 → 23 → 19 MiB |
| 500k × 64 B | 4.7 / 3.7 / 4.1 s | 72 MiB → 1.8 MiB | 87 → 43 → 30 MiB |
| 1M × 64 B | 8.8 / 8.4 / 7.8 s | 143 MiB → 1.7 MiB | 155 → **87** → 32 MiB |
| 1M × 1 KB | 11.8 / 9.6 / 9.2 s | 1,287 MiB → 1.7 MiB | 1,308 → 33 → 29 MiB |
| 1M × 4 KB | – / 13.0 / 14.0 s | 4,950 MiB → 1.7 MiB | 5,004 → 61 → 58 MiB |

* **H1 answer:** reclamation time grows linearly with the number of simultaneously expiring keys,
  at ~110–135k keys/s idle (64 B), and somewhat slower for larger values (1M × 4 KB: 13–14 s).
  The time limit (N2, N7) caps the expiry duty at ~20% of one core, so an idle server needs about
  5× longer than the raw deletion cost.
* `used_memory` falls back to the empty-server baseline as the keys go. It is an honest measure of
  *physically present* data, garbage included (F1).
* RSS: large values are returned to the OS quickly (5 GB → 61 MiB within 12 s; jemalloc background
  thread + decay). For many small values it lags: 1M × 64 B leaves 18–87 MiB of RSS (7.2.17 / 8.8.4 / 8.10.3:
  18 / 37 / 87 MiB, n = 1 each, vs ~1.5 MiB used) 12 s later, and `MEMORY PURGE` brings it to 18–32 MiB. That is allocator retention and
  fragmentation of small-object slabs, not expired keys.

**Profile** (`profile`, perf at 1999 Hz during the expiry window, 1M × 64 B, unmodified builds, LTO, so
inlined symbols are attributed approximately; `results/processed/profile_*.txt`). The expire cycle
splits roughly into **finding the keys**: walking buckets and touching each entry, ~30–38%
(`activeExpireCycle`/`expireScanCallback` + `dictScanDefragBucket`), mostly cache misses on
dict entries. Then **deleting**: `dbGenericDelete` → `dictTwoPhaseUnlinkFind` re-hashes the key and
searches the main keyspace dict, ~10%. Then **freeing**: `sdsfree`/`zfree`/`je_malloc_usable_size`/tcache
flushes, ~15–30%. Hashing (`siphash`) is ~1%. No single hotspot dominates. The cost per key is mostly
memory access, which fits the ~1.5–2 µs per key and its sensitivity to host noise.

## F8. During a BGSAVE, active expiry stops at ~1% and the freed memory stays pinned [undocumented]

*Suite `fork` (v185, 3 reps): 1M × 1 KB keys expire at T. A BGSAVE started 1 s before T is slowed
(`rdb-key-save-delay 60`) so the child is still alive ~95 s after T. Control: no BGSAVE.*

| version | no BGSAVE: time to remove all | with BGSAVE: keys stuck while the child runs | predicted stall point |
|---|---|---|---|
| 7.2.17 | 11.5–13.5 s | **10,469–10,478** (for ≥95 s) | 1% × 2^20 = 10,485 (no shrink while a child exists) |
| 8.8.4 | 9.5–10.5 s | **10,796–10,811** (for ≥95 s) | 1% × (2^20 + 2^15) = 10,813 |
| 8.10.3 | 10.0 s | **10,794–10,803** (for ≥95 s) | 1% × (2^20 + 2^15) = 10,813 |

* Same rule as F5, two flavours. 7.2.17 never resizes while a child exists (`databasesCron`), so the
  dict drops below 1% fill and is skipped. 8.x is allowed to start a 1/32 shrink under
  `DICT_RESIZE_AVOID` (`dict.c:413-418`), but cron rehashing is disabled while a child exists
  (`server.c:1331`). The shrink never progresses, and the old+new bucket count keeps the dict below
  1%. The stall lasts as long as the child does. With the F5 patch, 8.x would keep scanning the
  rehashing dict (not tested under fork).
* **Copy-on-write.** Deleting 1M keys touches every page that holds them. The child's private dirty
  memory reaches 1.09–1.12 GB, essentially a full copy of the dataset. Memory the parent frees is not
  returned to the OS while the child holds its copy: parent RSS + child private peaks at
  1.38–1.43 GB vs 1.31–1.33 GB before T. Mass expiry during a fork does not lower the footprint, and
  raises it 6–9% in these runs.

## F9. Version differences

* **#15412 (8.10.0)** matters only where the stale share sits between ~10% and the cliff: steady
  churn and partial expiry (F1, F2). It does not matter for simultaneous mass expiry (F4).
* **Per-key deletion cost, measured properly.** An early, block-ordered comparison (`effort` suite)
  suggested 7.2.17 was ~20% *cheaper* per key than 8.x and had a non-monotonic effort curve. Both
  were artefacts of host drift: the identical 7.2.17 configuration cost 1.19–1.26 µs/key at 22:23 and
  1.67–1.90 µs/key at 22:51 on the same host, and the suite ran versions and efforts in blocks. The
  round-robin re-test (`interleave`, v185, 6 rounds, all six arms in every round, ratios paired
  within a round) gives:

  | arm | µs per expired key | paired ratio vs 8.10.3 (same round) | time to remove 1M keys |
  |---|---|---|---|
  | 8.10.3 effort 1 | 1.72 [1.56–1.90] | 1.00 | 8.49 s [7.81–9.52] |
  | 8.8.4 effort 1 | 1.66 [1.46–1.98] | 1.00 [0.84–1.13] | 8.45 s [7.40–9.99] |
  | 7.2.17 effort 1 | 2.00 [1.94–2.10] | **1.17 [1.09–1.27]** | 10.15 s [9.85–10.73] |
  | 7.2.17 effort 4 | 2.00 [1.90–2.09] | 1.20 [1.10–1.26] | 8.71 s [8.27–8.98] |
  | 7.2.17 effort 7 | 1.83 [1.60–2.16] | 1.12 [0.84–1.23] | 7.38 s [6.23–8.32] |
  | 8.10.3 + F5 patch | 1.71 [1.63–1.84] | 1.02 [0.89–1.16] | 8.02 s [7.79–8.68] |

  So 8.x deletes an expired key ~15% more cheaply than 7.2.17, effort helps monotonically, and the
  F5 patch has no measurable CPU cost. Absolute µs/key drifted ~1.5× over the study on the same VM.
  Only paired, interleaved ratios are used for CPU-cost claims.

## F10. Documentation vs implementation [undocumented]

* The EXPIRE docs (redis/docs `content/commands/expire.md`, main @ `85cd0a83`) say Redis "tests a few
  keys at random". Since 6.0 (`ffc7e509a`) it walks a persistent bucket-scan cursor (N3). That is why
  F1 is a deterministic sweep with a sharp threshold rather than a decaying random sample.
* The slow cycle's "25% of CPU" is ~20% in practice. The cron is rescheduled `1000/hz` ms after the
  *end* of the previous tick (N7). Measured slow-cycle period during mass expiry: 126.1 ms median vs
  100.5 ms idle.

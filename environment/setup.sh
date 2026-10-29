#!/usr/bin/env bash
# Build every Redis binary used in this study from source.
#
# Plain (unmodified) builds:   7.2.17, 8.8.4, 8.10.3
# Research-only builds:        *-trace  (per-cycle activeExpireCycle trace, see instrumentation/)
#                              8.10.3-revert15412-trace (8.10.3 with the one-line fix of
#                              redis/redis#15412 reverted, to isolate its effect)
#
# Output: $WORK/bin/<name>/redis-server (+ redis-cli, redis-benchmark)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-/home/user/src}"
VERSIONS=(7.2.17 8.8.4 8.10.3)
mkdir -p "$WORK/bin"

pip install --break-system-packages -q redis hiredis numpy matplotlib >/dev/null 2>&1 || true
command -v perf >/dev/null || {
  apt-get update -qq && apt-get install -y -qq linux-tools-common linux-tools-generic >/dev/null
  ln -sf "$(ls -d /usr/lib/linux-tools-*/perf | head -1)" /usr/local/bin/perf
}

[ -d "$WORK/redis/.git" ] || git clone https://github.com/redis/redis.git "$WORK/redis"
cd "$WORK/redis"

build() { # <worktree> <name>
  (cd "$1" && make -j"$(nproc)" MALLOC=jemalloc > build.log 2>&1)
  mkdir -p "$WORK/bin/$2"
  cp "$1/src/redis-server" "$1/src/redis-cli" "$1/src/redis-benchmark" "$WORK/bin/$2/"
  git -C "$1" rev-parse HEAD > "$WORK/bin/$2/COMMIT"
  echo "built $2 @ $(cat "$WORK/bin/$2/COMMIT")"
}

for v in "${VERSIONS[@]}"; do
  [ -d "$WORK/redis-$v" ] || git worktree add -f "$WORK/redis-$v" "$v"
  build "$WORK/redis-$v" "$v"
  [ -d "$WORK/redis-$v-trace" ] || git worktree add -f "$WORK/redis-$v-trace" "$v"
  python3 "$HERE/instrumentation/apply_expire_trace.py" "$WORK/redis-$v-trace"
  build "$WORK/redis-$v-trace" "$v-trace"
done

# 8.10.3 with only the #15412 fix reverted (restores the pre-8.10 FAST-cycle gate).
R="$WORK/redis-8.10.3-revert15412-trace"
[ -d "$R" ] || git worktree add -f "$R" 8.10.3
sed -i 's/server.stat_expired_stale_perc \* 100 < config_cycle_acceptable_stale/server.stat_expired_stale_perc < config_cycle_acceptable_stale/' "$R/src/expire.c"
grep -q 'stat_expired_stale_perc < config_cycle_acceptable_stale' "$R/src/expire.c"
python3 "$HERE/instrumentation/apply_expire_trace.py" "$R"
build "$R" "8.10.3-revert15412-trace"

# Load generator (C, hiredis vendored by Redis)
make -C "$HERE/tools" REDIS_SRC="$WORK/redis-8.10.3"

# Candidate fix for finding F5 (research only): 8.10.3 + instrumentation/fix_skip_rehashing.patch + trace
F="$WORK/redis-8.10.3-fixskip-trace"
[ -d "$F" ] || git worktree add -f "$F" 8.10.3
git -C "$F" apply --check "$HERE/instrumentation/fix_skip_rehashing.patch" 2>/dev/null && \
  git -C "$F" apply "$HERE/instrumentation/fix_skip_rehashing.patch"
python3 "$HERE/instrumentation/apply_expire_trace.py" "$F"
build "$F" "8.10.3-fixskip-trace"

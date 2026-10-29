#!/bin/bash
# start|stop|status for the experiment runner (benchmarks/run_chain.sh), via a pid file.
cd "$(dirname "$0")/.."
PIDF=results/raw/.runner.pid
case "$1" in
  start)
    if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "already running"; exit 0; fi
    setsid nohup bash benchmarks/run_chain.sh >/dev/null 2>&1 < /dev/null &
    echo $! > $PIDF; echo "started $(cat $PIDF)";;
  stop)
    if [ -f $PIDF ]; then
      PG=$(ps -o pgid= -p "$(cat $PIDF)" 2>/dev/null | tr -d ' ')
      [ -n "$PG" ] && kill -- -"$PG" 2>/dev/null
      rm -f $PIDF
    fi
    for p in $(ps -eo pid,comm | awk '$2=="redis-server"{print $1}'); do kill -9 $p; done
    echo stopped;;
  status)
    if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo running; else echo "NOT running"; fi
    tail -1 results/raw/log_batch2.txt | cut -c1-100;;
esac

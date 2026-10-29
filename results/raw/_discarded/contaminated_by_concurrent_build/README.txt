Runs executed between 2026-10-28 23:09:59 and 23:18 UTC, while the 8.10.3-fixskip-trace build
(gcc -flto) was compiling on CPU 3. Although redis-server was pinned to CPU 1, these runs were
1.4-1.6x slower than identical runs from other suites (shared caches/memory bandwidth/vCPU
contention on the VM). They are excluded from all analysis and were re-run on an otherwise idle machine.

#!/usr/bin/env python
"""Benchmark argon2id parameters. Run with: uv run bench_argon2.py"""

import time

from argon2 import PasswordHasher

PASSWORD = "hunter2correcthorsebatterystaple"

CONFIGS = [
    # (label, memory_cost_KB, time_cost, parallelism)
    ("64 MB  t=2", 65_536, 2, 1),
    ("64 MB  t=3", 65_536, 3, 1),
    ("100 MB t=2", 102_400, 2, 1),
    ("100 MB t=3", 102_400, 3, 1),
    ("128 MB t=2", 131_072, 2, 1),
]

ROUNDS = 3

print(f"{'Config':<16} {'avg ms':>8} {'min ms':>8} {'max ms':>8}")
print("-" * 44)

for label, m, t, p in CONFIGS:
    ph = PasswordHasher(memory_cost=m, time_cost=t, parallelism=p)
    times = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        ph.verify(ph.hash(PASSWORD), PASSWORD)
        times.append((time.perf_counter() - start) * 1000)
    print(f"{label:<16} {sum(times) / len(times):>8.1f} {min(times):>8.1f} {max(times):>8.1f}")

"""F3 — the crash-visible cycle marker has no cross-process lock (ROOT-CAUSE S4).

``CycleMarker.begin`` is a decide-then-record on a shared file:

    existing = self._read()          # decide: is a fresh cycle already running?
    ... freshness check ...
    self._write({...})               # record: claim it

Nothing serialises the read against the write across processes. The in-process
``asyncio.Lock`` in ``poe_cycle_op`` cannot see another OS process, so the marker
is the ONLY cross-process guard — and it does not guard.

This test spawns N real processes that all claim the *same* switch's marker at
once. If any cross-process mutual exclusion existed, exactly one would get "OK".
Instead every one does, disproving "never runs two cycles at once (... a
crash-visible marker ...)" for the multi-process case.

(The worker's ``SlowMarker`` only slows the write so the inherent race is
deterministic; it adds no lock — see ``mp_cycle_worker``.)
"""

from __future__ import annotations

import multiprocessing as mp

import mp_cycle_worker  # bare-name import; conftest puts this dir on sys.path

WORKERS = 6


def test_two_processes_both_claim_the_same_marker(tmp_path) -> None:
    ctx = mp.get_context("spawn")
    state_dir = str(tmp_path)
    device = "192.0.2.10"
    started = 1000.0
    barrier = ctx.Barrier(WORKERS)

    procs = [
        ctx.Process(
            target=mp_cycle_worker.run,
            args=(state_dir, device, i, barrier, started),
        )
        for i in range(WORKERS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    results = [
        (tmp_path / f"result-{i}.txt").read_text(encoding="utf-8").strip()
        if (tmp_path / f"result-{i}.txt").exists()
        else "MISSING"
        for i in range(WORKERS)
    ]
    wins = results.count("OK")

    # CLAIMED INVARIANT: "never runs two cycles at once (... a crash-visible marker ...)".
    # A correct cross-process guard admits EXACTLY ONE cycle; the rest see
    # CYCLE_IN_PROGRESS. FAILS today: with no file lock, every process wins.
    assert wins == 1, (
        f"exactly one process may claim the marker; got {wins} winners. results={results}"
    )

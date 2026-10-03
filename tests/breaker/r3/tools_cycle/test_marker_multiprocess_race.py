"""TC-F3 (closed by X1b) - the cycle reservation IS cross-process serialised.

The old ``CycleMarker.begin`` was a lock-free decide-then-record on a shared file,
so N processes could all read "no cycle" and all start. The X1b ``CycleGuard`` is a
thin policy over the canonical ``ReservationStore``: reserving takes a per-key
cross-process advisory lock and writes ``reserved=1`` under it before returning, so
the read and the write are one atomic step.

This test spawns N real processes that all claim the *same* port's reservation at
once. Exactly one gets "OK"; the rest see ``CycleInProgress``. (Previously xfail:
"deferred to X1b ReservationStore" - now implemented, so it must PASS.)
"""

from __future__ import annotations

import multiprocessing as mp

import mp_cycle_worker  # bare-name import; conftest puts this dir on sys.path

WORKERS = 6


def test_only_one_process_claims_the_same_port(tmp_path) -> None:
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

    # A correct cross-process guard admits EXACTLY ONE cycle; the rest see
    # CYCLE_IN_PROGRESS. The flock serialises the read-modify-write, so there is no
    # lost update and no second winner.
    assert results.count("OK") == 1, f"exactly one process may claim the port; results={results}"
    assert results.count("BLOCKED") == WORKERS - 1, results

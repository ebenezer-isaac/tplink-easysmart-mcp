"""Top-level worker for the multiprocessing cycle-reservation test (spawn-safe).

Must live in its own importable module so a ``spawn`` child can import the target
by name on Windows. Each worker claims the *same* switch port's cycle reservation
through the real :class:`CycleGuard.reserve` (a thin policy over the canonical
``ReservationStore``) and records whether it was allowed to proceed.

Post X1b, the reservation takes the per-key cross-process advisory lock and writes
``reserved=1`` under it before returning, so exactly one worker may claim a given
port; the rest see ``CycleInProgress``. The winner holds the reservation (never
releases, as a running cycle would) by simply exiting with it unresolved.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The editable install puts tplink_easysmart_mcp on sys.path; keep the child robust.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from tplink_easysmart_mcp.switch.cycle_guard import CycleGuard
from tplink_easysmart_mcp.switch.errors import CycleInProgress


def run(state_dir: str, device: str, idx: int, barrier, started: float) -> None:
    guard = CycleGuard(state_dir, device, now=lambda: started)
    result_path = Path(state_dir) / f"result-{idx}.txt"
    try:
        barrier.wait(timeout=30)
    except Exception:  # pragma: no cover - a barrier timeout is itself a failure signal
        result_path.write_text("BARRIER_TIMEOUT", encoding="utf-8")
        return
    try:
        guard.reserve(port=1, name="cam1")  # held, never released (as a live cycle would)
        outcome = "OK"  # this worker claimed the port
    except CycleInProgress:
        outcome = "BLOCKED"
    except Exception as exc:  # pragma: no cover - surface any unexpected failure
        outcome = f"ERROR:{type(exc).__name__}"
    result_path.write_text(outcome, encoding="utf-8")
    os.sync() if hasattr(os, "sync") else None

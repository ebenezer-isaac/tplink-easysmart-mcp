"""Top-level worker for the multiprocessing marker-race test (spawn-safe).

Must live in its own importable module so a ``spawn`` child can import the target
by name on Windows. Each worker claims the *same* switch's cycle marker through
the real :class:`CycleMarker.begin` and records whether it was allowed to proceed.

``SlowMarker`` overrides only ``_write`` to add latency; the claim-under-test
(``begin`` is atomic / serialised across processes) lives entirely in the
inherited ``begin``/``_read``/``_write`` sequence. The latency does not add or
remove any lock — it only widens the existing read-then-write window so the race
is deterministic. If any cross-process mutual exclusion existed, exactly one
worker would win regardless of write latency.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# The editable install puts tplink_easysmart_mcp on sys.path; keep the child robust.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from tplink_easysmart_mcp.switch.cycle import CycleMarker
from tplink_easysmart_mcp.switch.errors import CycleInProgress


class SlowMarker(CycleMarker):
    """Real ``begin`` logic; only the write step is slowed to expose the race."""

    def _write(self, payload: dict) -> None:  # type: ignore[override]
        time.sleep(0.4)
        super()._write(payload)


def run(state_dir: str, device: str, idx: int, barrier, started: float) -> None:
    marker = SlowMarker(state_dir, device, now=lambda: started)
    result_path = Path(state_dir) / f"result-{idx}.txt"
    try:
        barrier.wait(timeout=30)
    except Exception:  # pragma: no cover - a barrier timeout is itself a failure signal
        result_path.write_text("BARRIER_TIMEOUT", encoding="utf-8")
        return
    try:
        marker.begin(port=1, name="cam1")
        outcome = "OK"  # this worker believes it may run the cycle
    except CycleInProgress:
        outcome = "BLOCKED"
    except Exception as exc:  # pragma: no cover - surface any unexpected failure
        outcome = f"ERROR:{type(exc).__name__}"
    result_path.write_text(outcome, encoding="utf-8")
    os.sync() if hasattr(os, "sync") else None

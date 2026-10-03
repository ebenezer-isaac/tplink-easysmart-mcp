"""Per-port 'a power-cycle is in progress' reservation over the core store.

Replaces the old lock-free, un-clamped ``CycleMarker`` (breaker findings TC-F3 /
TC-F4) with a thin :class:`~..core.slot.SingleSlot` over the canonical
:class:`~..core.state.ReservationStore`, exactly as ``LoginBreaker`` is and exactly
as vigi's ``export_lock`` is. One reservation per ``(device, port)`` key
(``cycle-<device>-p<port>``):

* **One cycle in flight per port, serialised across processes.** ``SingleSlot``
  admits a reservation only when no live one exists; the primitive inserts the named
  slot under the per-key cross-process advisory lock, so the read-then-write is one
  atomic step and two processes cannot both claim the same port (TC-F3).
* **A crashed cycle stays visible and refuses.** A process that dies mid-cycle never
  releases, so its slot stays on disk; :meth:`status` reports it and a new cycle on
  that port is refused until it is cleared or ages out.
* **Clock-clamped 5-minute stale rule, inside the primitive.** A slot blocks only
  while its age is in ``[0, stale_after_s)``. A future-dated or backward-clock
  ``reserved_at`` yields a negative age — dropped as stale by the primitive before
  admit, never "fresh forever" (TC-F4) — and an age past the window is stale too.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.breaker import _safe
from ..core.errors import StateUnavailable
from ..core.slot import SingleSlot, SingleSlotLedger, single_slot_store
from ..core.state import Reservation, ReservationStore
from .errors import CycleInProgress

CYCLE_STALE_AFTER_S = 300.0

_CYCLE_MESSAGE = (
    "a power-cycle is already in progress on this port; refusing to start a second cycle"
)


class CycleGuard:
    """Per-port cross-process cycle reservations for one device, as thin SingleSlots."""

    def __init__(
        self,
        state_dir: str | Path,
        device_key: str,
        *,
        now: Callable[[], float] = time.time,
        stale_after_s: float = CYCLE_STALE_AFTER_S,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._device_key = device_key
        self._now = now
        self._stale = float(stale_after_s)
        self._prefix = f"cycle-{_safe(device_key)}-p"
        self._store: ReservationStore[SingleSlotLedger] = single_slot_store(
            self._state_dir, clock=now
        )

    def _key(self, port: int) -> str:
        return f"{self._prefix}{port}"

    def path(self, port: int) -> Path:
        """The ledger path for ``port`` (``<key>.json``). Used by tests and status."""
        return self._store.path(self._key(port))

    def _slot(self, port: int) -> SingleSlot:
        return SingleSlot(
            self._store,
            self._key(port),
            stale_after_s=self._stale,
            clock=self._now,
            reason="CYCLE_IN_PROGRESS",
            message=_CYCLE_MESSAGE,
            refuse=self._refuse,
        )

    def _refuse(self, message: str, reason: str, context: dict[str, Any]) -> Exception:
        """Turn the primitive's refusal into the switch's ``CycleInProgress`` envelope."""
        since = context.get("since_epoch")
        started_at = float(since) if since is not None else None
        age = (float(self._now()) - started_at) if started_at is not None else None
        return CycleInProgress(
            message,
            started_at=started_at,
            age_s=round(age, 3) if age is not None else None,
        )

    def reserve(self, port: int, name: str | None) -> Reservation:
        """Reserve the cycle for ``port`` or raise :class:`CycleInProgress`.

        Raises if a live reservation already holds the port (cross-process) or the
        store is unusable (fail closed). A stale reservation is reclaimed by the
        primitive before admission.
        """
        try:
            return self._slot(port).reserve(meta={"port": port, "name": name, "pid": os.getpid()})
        except StateUnavailable as exc:
            raise CycleInProgress(
                f"the cycle reservation store is unusable, so a concurrent cycle cannot be "
                f"ruled out; refusing (fail closed). {exc}"
            ) from exc

    def status(self) -> dict[str, Any]:
        """A non-secret snapshot of any in-progress cycle reservations. Never raises."""
        in_progress: list[dict[str, Any]] = []
        for path in sorted(self._state_dir.glob(f"{self._prefix}*.json")):
            key = path.name.removesuffix(".json")
            try:
                ledger = self._store.load(key)
            except (StateUnavailable, ValueError):
                in_progress.append({"path": str(path), "reserved": None, "reason": "unreadable"})
                continue
            if ledger is None or not ledger.reservations:
                continue
            slot = next(iter(ledger.reservations.values()))
            started_at = slot.reserved_at
            age = float(self._now()) - started_at
            in_progress.append(
                {
                    "port": slot.meta.get("port"),
                    "name": slot.meta.get("name"),
                    "started_at": started_at,
                    "age_s": round(age, 3),
                    "stale": not (0.0 <= age < self._stale),
                }
            )
        return {"in_progress": bool(in_progress), "reservations": in_progress}

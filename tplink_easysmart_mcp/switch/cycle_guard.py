"""Per-port 'a power-cycle is in progress' reservation over the core store.

Replaces the old lock-free, un-clamped ``CycleMarker`` (breaker findings TC-F3 /
TC-F4) with a thin policy over the canonical :class:`ReservationStore`, exactly as
``LoginBreaker`` is. One reservation per ``(device, port)``:

* **One cycle in flight per port, serialised across processes.** Reserving takes the
  per-key cross-process advisory lock and writes ``reserved=1`` under it before
  returning, so two processes cannot both claim the same port — there is no
  lock-free read-then-write (TC-F3).
* **A crashed cycle stays visible and refuses.** A process that dies mid-cycle never
  releases, so ``reserved`` stays ``1`` on disk; :meth:`status` reports it and a new
  cycle on that port is refused until it is cleared or ages out.
* **Clock-clamped 5-minute stale rule, inside the policy.** A reservation blocks only
  while its age is in ``[0, stale_after_s)``. A future-dated or backward-clock
  ``started_at`` yields a negative age — treated as stale (recover), never
  "fresh forever" (TC-F4) — and an age past the window is stale too.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.breaker import _safe
from ..core.errors import StateUnavailable
from ..core.state import Outcome, Reservation, ReservationStore
from .errors import CycleInProgress

CYCLE_STALE_AFTER_S = 300.0
SCHEMA_VERSION = 1


class CycleLedger(BaseModel):
    """The one strictly-typed shape of a per-port cycle reservation."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1] = SCHEMA_VERSION
    key: str
    reserved: int = Field(ge=0)
    port: int | None
    name: str | None
    started_at: float | None
    last_update: float


class CycleGuard:
    """Per-port cross-process cycle reservations for one device."""

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

    def _store(self, port: int) -> ReservationStore[CycleLedger]:
        return ReservationStore(
            self._state_dir,
            f"{self._prefix}{port}",
            CycleLedger,
            make_default=lambda: self._fresh(port),
        )

    def _fresh(self, port: int) -> CycleLedger:
        return CycleLedger(
            key=self._device_key,
            reserved=0,
            port=port,
            name=None,
            started_at=None,
            last_update=float(self._now()),
        )

    def _fresh_age(self, started_at: float | None) -> float | None:
        """Age in seconds, or ``None`` if unknown. Not clamped (the caller decides)."""
        if started_at is None:
            return None
        return float(self._now()) - float(started_at)

    def _is_fresh(self, started_at: float | None) -> bool:
        """A reservation blocks only while its age is within ``[0, stale_after_s)``.

        A negative age (future/backward-clock ``started_at``) or an age past the
        window is stale, so a bad timestamp can never wedge the port forever.
        """
        age = self._fresh_age(started_at)
        return age is not None and 0.0 <= age < self._stale

    def reserve(self, port: int, name: str | None) -> Reservation:
        """Reserve the cycle for ``port`` or raise :class:`CycleInProgress`.

        Raises if a fresh reservation already holds the port (cross-process) or the
        store is unusable (fail closed). A stale reservation is taken over.
        """
        store = self._store(port)

        def admit(ledger: CycleLedger) -> CycleLedger:
            if ledger.reserved >= 1 and self._is_fresh(ledger.started_at):
                age = self._fresh_age(ledger.started_at)
                raise CycleInProgress(
                    "a power-cycle is already in progress on this port; refusing to start "
                    "a second cycle",
                    started_at=ledger.started_at,
                    age_s=round(age, 3) if age is not None else None,
                )
            now = float(self._now())
            return ledger.model_copy(
                update={
                    "reserved": 1,
                    "port": port,
                    "name": name,
                    "started_at": now,
                    "last_update": now,
                }
            )

        def resolve(ledger: CycleLedger, outcome: Outcome, kwargs: dict[str, Any]) -> CycleLedger:
            return ledger.model_copy(
                update={"reserved": 0, "started_at": None, "last_update": float(self._now())}
            )

        try:
            return store.reserve(admit, resolve)
        except StateUnavailable as exc:
            raise CycleInProgress(
                f"the cycle reservation store is unusable, so a concurrent cycle cannot be "
                f"ruled out; refusing (fail closed). {exc}"
            ) from exc

    def status(self) -> dict[str, Any]:
        """A non-secret snapshot of any in-progress cycle reservations. Never raises."""
        in_progress: list[dict[str, Any]] = []
        for path in sorted(self._state_dir.glob(f"{self._prefix}*.json")):
            try:
                ledger = self._store_for_file(path).load()
            except (StateUnavailable, ValueError):
                in_progress.append({"path": str(path), "reserved": None, "reason": "unreadable"})
                continue
            if ledger is None or ledger.reserved < 1:
                continue
            age = self._fresh_age(ledger.started_at)
            in_progress.append(
                {
                    "port": ledger.port,
                    "name": ledger.name,
                    "started_at": ledger.started_at,
                    "age_s": round(age, 3) if age is not None else None,
                    "stale": not self._is_fresh(ledger.started_at),
                }
            )
        return {"in_progress": bool(in_progress), "reservations": in_progress}

    def _store_for_file(self, path: Path) -> ReservationStore[CycleLedger]:
        name = path.name.removesuffix(".json")
        return ReservationStore(
            self._state_dir, name, CycleLedger, make_default=lambda: self._fresh(0)
        )

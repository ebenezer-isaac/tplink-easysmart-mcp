"""The crash-visible 'a power-cycle is in progress' marker file for one switch.

A marker younger than ``MARKER_STALE_AFTER_S`` refuses a second cycle
(``CYCLE_IN_PROGRESS``); an older one is reported as stale and overwritten. The
cross-process lock-free read-then-write and the un-clamped freshness clock
(breaker findings TC-F3 / TC-F4) are deferred to X1b's ``ReservationStore`` by
orchestrator decision; this module keeps the single-process behaviour unchanged.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..core.breaker import Clock, _slug
from .errors import CycleInProgress

MARKER_STALE_AFTER_S = 300


class CycleMarker:
    """A crash-visible 'a cycle is in progress' marker file for one switch."""

    def __init__(
        self, state_dir: str | os.PathLike[str], device: str, *, now: Clock = time.time
    ) -> None:
        self._path = Path(state_dir) / f"cycle-{_slug(device)}.json"
        self._now = now

    @property
    def path(self) -> Path:
        return self._path

    def begin(self, port: int, name: str | None) -> None:
        """Claim the marker, or raise ``CycleInProgress`` if a fresh one exists."""
        existing = self._read()
        if existing is not None:
            started_at = existing.get("started_at")
            age = self._now() - started_at if isinstance(started_at, int | float) else None
            if age is not None and age < MARKER_STALE_AFTER_S:
                raise CycleInProgress(
                    "a power-cycle marker is already present and still fresh; refusing to start "
                    "a second cycle",
                    started_at=started_at,
                    age_s=round(age, 3),
                )
        self._write({"started_at": self._now(), "port": port, "name": name})

    def end(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    def snapshot(self) -> dict[str, Any]:
        existing = self._read()
        if existing is None:
            return {"in_progress": False}
        started_at = existing.get("started_at")
        age = self._now() - started_at if isinstance(started_at, int | float) else None
        return {
            "in_progress": True,
            "started_at": started_at,
            "age_s": round(age, 3) if age is not None else None,
            "stale": age is not None and age >= MARKER_STALE_AFTER_S,
            "port": existing.get("port"),
            "name": existing.get("name"),
        }

    def _read(self) -> dict[str, Any] | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            # Unreadable: surface it as a stale marker (allow recovery), not a lock.
            return {"started_at": None}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"started_at": None}
        return data if isinstance(data, dict) else {"started_at": None}

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".cycle-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

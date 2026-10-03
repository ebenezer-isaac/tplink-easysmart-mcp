"""Persistent login circuit breaker (device-agnostic, file-backed, fail-closed).

Many embedded devices lock the admin account after a handful of failed logins.
The breaker records a single durable fact — "a login has failed, do not try
again until a human intervenes" — in a JSON file under the device's state dir, so
the decision survives a process restart (the copied in-process version lost it).

Safety rules:

* The state file is written atomically (temp file + ``os.replace``) and chmod
  ``0600`` so a password-adjacent detail can never be world-readable.
* **Fail closed.** A state file that is present but unreadable or corrupt is
  treated as *open*: if we cannot prove the breaker is clear, we refuse to log
  in. Only an absent file (never tripped) or an explicit ``open: false`` lets a
  login through.
* Clearing is an explicit human action (``<console-script> breaker --clear``).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import LockoutGuard

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(device: str) -> str:
    cleaned = _UNSAFE.sub("_", device.strip()) or "device"
    return cleaned[:64]


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


@dataclass(frozen=True)
class BreakerState:
    open: bool
    corrupt: bool = False
    opened_at: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe, secret-free view for the healthcheck and the CLI."""
        return {
            "open": self.open,
            "corrupt": self.corrupt,
            "opened_at": self.opened_at,
            "details": dict(self.details),
        }


class LoginBreaker:
    """A persistent, fail-closed login breaker for one device."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        device: str,
        *,
        clear_hint: str = "clear it after fixing the cause",
    ) -> None:
        self._path = Path(state_dir) / f"breaker-{_slug(device)}.json"
        self._clear_hint = clear_hint

    @property
    def path(self) -> Path:
        return self._path

    def state(self) -> BreakerState:
        """Read the breaker's current state. Unreadable/corrupt ⇒ open (fail closed)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return BreakerState(open=False)
        except OSError:
            return BreakerState(
                open=True, corrupt=True, details={"reason": "unreadable state file"}
            )
        try:
            data = json.loads(raw)
        except ValueError:
            return BreakerState(open=True, corrupt=True, details={"reason": "state file corrupt"})
        if not isinstance(data, dict) or not isinstance(data.get("open"), bool):
            return BreakerState(open=True, corrupt=True, details={"reason": "state file malformed"})
        if not data["open"]:
            return BreakerState(open=False)
        opened_at = data.get("opened_at")
        return BreakerState(
            open=True,
            opened_at=opened_at if isinstance(opened_at, int | float) else None,
            details=dict(data["details"]) if isinstance(data.get("details"), dict) else {},
        )

    @property
    def is_open(self) -> bool:
        return self.state().open

    def check(self) -> None:
        """Raise ``LockoutGuard`` if the breaker is open. No I/O beyond the read."""
        state = self.state()
        if not state.open:
            return
        reason = (
            state.details.get("reason") or state.details.get("code") or "a previous login failed"
        )
        raise LockoutGuard(
            f"Login refused: the circuit breaker is open ({reason}). "
            f"The account may be locked on the device; {self._clear_hint}."
        )

    def record_failure(self, details: dict[str, Any] | None = None) -> None:
        """Trip the breaker and persist why. Idempotent; overwrites any prior state."""
        payload = {
            "open": True,
            "opened_at": time.time(),
            "details": {k: _json_safe(v) for k, v in (details or {}).items()},
        }
        self._atomic_write(payload)

    def clear(self) -> None:
        """Reset the breaker (an explicit human action). A no-op if already clear."""
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".breaker-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# Callable clock type, re-exported for the switch cooldown that mirrors this module.
Clock = Callable[[], float]

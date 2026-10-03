"""``switch_poe_cycle``: power-cycle one PoE camera safely.

The dangerous part of a cycle is the window where the port is OFF: a crash, an
eviction or a tripped breaker must never leave a camera dark silently. So:

* Only one cycle runs per backend at a time — a non-blocking in-process
  ``asyncio.Lock`` plus a **state-file marker** under ``EASYSMART_STATE_DIR`` that
  survives a crash. A second call while a marker is fresh (< 5 min) is refused as
  ``CYCLE_IN_PROGRESS``; a marker older than that is reported as stale and the new
  cycle is allowed to proceed (it overwrites it).
* Once the port has been turned OFF, **every** failure path still attempts to turn
  it back ON and reports both outcomes (``CYCLE_INCOMPLETE`` naming the port that
  may be UNPOWERED).
* After re-enabling, power draw is polled until the PD actually pulls current;
  PoE-on-but-no-draw is ``POWER_NOT_RESTORED`` (not a success).

The sleep and the clock are injected (``backend.sleep`` / ``backend.now``) so the
whole state machine runs under a fake clock in tests with no real waiting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..core.breaker import Clock, _slug
from ..core.errors import DeviceError
from .config import MAX_PORT, SwitchSettings
from .errors import (
    AlreadyOff,
    CycleIncomplete,
    CycleInProgress,
    InvalidArgument,
    PowerNotRestored,
    WriteVerifyFailed,
)
from .forms import RequestPlan, build_poe_port_form, plan_redacted
from .models import PoePort, PoeStatus
from .tools_read import poe_view, resolve_port
from .tools_write import refuse_non_poe_config, refuse_non_poe_live, refuse_protected

if TYPE_CHECKING:
    from .backend import EasySmartSwitchBackend

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2
MARKER_STALE_AFTER_S = 300


class PoeCycleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port_or_name: int | str


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
            log.warning(
                "stale power-cycle marker (age %ss) found; a previous cycle likely crashed — "
                "proceeding and overwriting it",
                round(age, 3) if age is not None else "unknown",
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


def _validate_off_seconds(value: object, settings: SwitchSettings) -> int:
    lo, hi = settings.cycle_off_min_s, settings.cycle_off_max_s
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(
            f"off_seconds must be a whole number between {lo} and {hi}",
            argument="off_seconds",
            allowed={"min": lo, "max": hi},
        )
    if not lo <= value <= hi:
        raise InvalidArgument(
            f"off_seconds={value} is out of range {lo}..{hi}",
            argument="off_seconds",
            allowed={"min": lo, "max": hi},
        )
    return value


async def poe_cycle_op(
    backend: EasySmartSwitchBackend, *, port_or_name: int | str, off_seconds: object = 10
) -> dict[str, Any]:
    settings = backend.settings
    args = PoeCycleInput(port_or_name=port_or_name)
    off = _validate_off_seconds(off_seconds, settings)
    resolved = resolve_port(args.port_or_name, settings=settings, max_port=MAX_PORT)
    refuse_non_poe_config(settings, resolved)
    refuse_protected(settings, resolved)
    if backend.cycle_lock.locked():
        raise CycleInProgress("another power-cycle is already running on this switch")
    async with backend.cycle_lock:
        backend.cycle_marker.begin(resolved.port, resolved.name)
        try:
            return await _run_cycle(backend, resolved.port, resolved.name, off)
        finally:
            backend.cycle_marker.end()


async def _run_cycle(
    backend: EasySmartSwitchBackend, port: int, name: str | None, off_seconds: int
) -> dict[str, Any]:
    settings = backend.settings
    client = backend.client
    now = backend.now
    async with client.session_scope(logout_after=True):
        snap = await client.poe()
        backend.observe_poe_port_num(snap.poe_port_num)
        from .tools_read import ResolvedPort

        refuse_non_poe_live(settings, ResolvedPort(port, name), snap.poe_port_num)
        before = snap.ports[port - 1]
        before_view = poe_view(settings, before)
        if not before.enabled:
            raise AlreadyOff(
                f"port {port} ({name}) PoE is already off; nothing to cycle",
                port=port,
                name=name,
                state=before_view,
            )
        off_form = build_poe_port_form(before, False)
        on_form = build_poe_port_form(before, True)
        if settings.dry_run:
            return {
                "dry_run": True,
                "port": port,
                "name": name,
                "off_seconds": off_seconds,
                "before": before_view,
                "planned": {
                    "off": plan_redacted(off_form).as_dict(),
                    "on": plan_redacted(on_form).as_dict(),
                    "sleep_s": off_seconds,
                },
            }
        # --- turn off (harmless if it fails: the port is still on) ---
        await client.submit(off_form)
        off_state = (await client.poe()).ports[port - 1]
        if off_state.enabled:
            raise WriteVerifyFailed(
                f"port {port} ({name}) PoE did not turn off; the cycle was aborted before any "
                "outage",
                before=before_view,
                after=poe_view(settings, off_state),
            )
        off_at = now()
        # --- from here the port is OFF: every failure must still attempt ON ---
        try:
            await backend.sleep(off_seconds)
            after = await _turn_on_with_retry(backend, port, name, on_form, before_view)
            on_at = now()
            powered, last = await _poll_power(backend, port, on_at)
            if not powered:
                raise PowerNotRestored(
                    f"port {port} ({name}): PoE is ON but the device drew no power within "
                    f"{settings.cycle_power_timeout_s}s",
                    context={"port": port, "name": name, "poe": poe_view(settings, last)},
                )
            return {
                "port": port,
                "name": name,
                "outcome": "cycled",
                "off_for_s": round(on_at - off_at, 3),
                "off_at": off_at,
                "on_at": on_at,
                "powered_at": now(),
                "power_w": last.power_w,
                "pd_class": last.pd_class,
                "before": before_view,
                "after": poe_view(settings, after),
            }
        except (PowerNotRestored, CycleIncomplete):
            raise  # the port is ON (PowerNotRestored) or ON was already retried
        except DeviceError as exc:
            on_outcome = await _best_effort_on(backend, on_form)
            raise CycleIncomplete(
                f"port {port} ({name}) may be UNPOWERED: the cycle failed after the port was "
                f"turned off ({exc})",
                context={
                    "port": port,
                    "name": name,
                    "off_outcome": "ok",
                    "on_outcome": on_outcome,
                    "error": getattr(exc, "kind", "DEVICE_ERROR"),
                },
            ) from exc


async def _turn_on_with_retry(
    backend: EasySmartSwitchBackend,
    port: int,
    name: str | None,
    on_form: RequestPlan,
    before_view: dict[str, Any],
) -> PoePort:
    client = backend.client
    settings = backend.settings
    await client.submit(on_form)
    after = (await client.poe()).ports[port - 1]
    if after.enabled:
        return after
    # One fresh read, then exactly one more ON attempt.
    after = (await client.poe()).ports[port - 1]
    if after.enabled:
        return after
    await client.submit(on_form)
    after = (await client.poe()).ports[port - 1]
    if after.enabled:
        return after
    raise CycleIncomplete(
        f"port {port} ({name}) may be UNPOWERED: PoE did not turn back on after one retry",
        context={
            "port": port,
            "name": name,
            "off_outcome": "ok",
            "on_outcome": "failed",
            "before": before_view,
            "after": poe_view(settings, after),
        },
    )


async def _poll_power(
    backend: EasySmartSwitchBackend, port: int, start_t: float
) -> tuple[bool, PoePort]:
    client = backend.client
    deadline = start_t + backend.settings.cycle_power_timeout_s
    last = (await client.poe()).ports[port - 1]
    while True:
        if last.status is PoeStatus.ON and last.power_w > 0:
            return True, last
        if backend.now() >= deadline:
            return False, last
        await backend.sleep(POLL_INTERVAL_S)
        last = (await client.poe()).ports[port - 1]


async def _best_effort_on(backend: EasySmartSwitchBackend, on_form: RequestPlan) -> str:
    """Try once more to re-enable PoE, swallowing any error, and report the outcome."""
    try:
        await backend.client.submit(on_form)
    except DeviceError as exc:
        return f"failed:{getattr(exc, 'kind', 'DEVICE_ERROR')}"
    except Exception:
        log.warning("best-effort re-enable raised an unexpected error", exc_info=True)
        return "failed"
    return "attempted"

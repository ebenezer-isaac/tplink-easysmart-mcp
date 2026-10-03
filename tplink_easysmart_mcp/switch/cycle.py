"""``switch_poe_cycle``: power-cycle one PoE camera safely.

The dangerous part of a cycle is the window where the port is OFF: a crash, an
eviction, a tripped breaker, a cancellation or any other failure must never leave
a camera dark silently. So the moment the OFF form is submitted a ``try/finally``
takes over and **guarantees a restore attempt** on every exit path:

* Only one cycle runs per backend at a time — a non-blocking in-process
  ``asyncio.Lock`` plus a **state-file marker** under ``EASYSMART_STATE_DIR`` that
  survives a crash. A second call while a marker is fresh (< 5 min) is refused as
  ``CYCLE_IN_PROGRESS``; a marker older than that is reported as stale and the new
  cycle is allowed to proceed (it overwrites it).
* From the instant the OFF form is submitted, the ``finally`` re-runs the ON
  sequence (submit + verify, with its own single re-login allowance and its own
  error capture) on **every** exit — a normal return, a ``DeviceError``, an
  ``OutcomeUnknown`` connection reset, ``asyncio.CancelledError``,
  ``KeyboardInterrupt`` or any other ``BaseException``. Cancellation and keyboard
  interrupts are re-raised *after* the restore attempt; every other failure
  becomes a ``CYCLE_INCOMPLETE`` envelope reporting ``off_outcome``,
  ``on_outcome``, ``port_state_after`` (from a final re-read when possible) and
  ``may_be_unpowered``.
* A connection reset on the OFF submit or the OFF verify is reported honestly
  (``OUTCOME_UNKNOWN`` for the off step) and the port is still driven back on.
* After re-enabling, power draw is polled until the PD actually pulls current;
  PoE-on-but-no-draw is ``POWER_NOT_RESTORED`` (not a success).

The sleep and the clock are injected (``backend.sleep`` / ``backend.now``) so the
whole state machine runs under a fake clock in tests with no real waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..core.errors import DeviceError, TransportError
from .config import MAX_PORT, SwitchSettings
from .errors import (
    AlreadyOff,
    CycleIncomplete,
    CycleInProgress,
    InvalidArgument,
    OutcomeUnknown,
    PowerNotRestored,
    WriteVerifyFailed,
)
from .forms import RequestPlan, build_poe_port_form, plan_redacted
from .marker import CycleMarker
from .models import PoePort, PoeStatus
from .tools_read import poe_view, resolve_port
from .tools_write import refuse_non_poe_config, refuse_non_poe_live, refuse_protected

if TYPE_CHECKING:
    from .backend import EasySmartSwitchBackend

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2

# Re-exported so ``backend`` / the breaker tests keep importing it from ``cycle``.
__all__ = ["CycleMarker", "PoeCycleInput", "poe_cycle_op"]


class PoeCycleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port_or_name: int | str


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
        return await _off_then_on(backend, port, name, off_seconds, before_view, off_form, on_form)


async def _off_then_on(
    backend: EasySmartSwitchBackend,
    port: int,
    name: str | None,
    off_seconds: int,
    before_view: dict[str, Any],
    off_form: RequestPlan,
    on_form: RequestPlan,
) -> dict[str, Any]:
    """Submit OFF, then guarantee an ON restore attempt in ``finally`` on every exit."""
    settings = backend.settings
    client = backend.client
    now = backend.now

    off_outcome = "ok"  # "ok" (confirmed) | "unknown" (connection reset)
    off_at: float | None = None
    off_took = False  # the off-verify read confirmed the port actually went off
    off_view: dict[str, Any] | None = None
    cancel: BaseException | None = None
    phase_error: BaseException | None = None
    on_report: _OnReport | None = None

    try:
        # --- the OFF form is submitted: the danger window is now open ---
        try:
            await client.submit(off_form)
        except OutcomeUnknown:
            # The POST landed-or-not; never resend. Settle by the restore's re-read.
            off_outcome = "unknown"
        else:
            off_state = (await client.poe()).ports[port - 1]
            off_view = poe_view(settings, off_state)
            off_took = not off_state.enabled
            if off_took:
                off_at = now()
                await backend.sleep(off_seconds)
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        cancel = exc
    except TransportError as exc:
        # The connection to the switch dropped mid-cycle (e.g. the off-verify read
        # was reset); we can no longer confirm the off, and the session is gone.
        phase_error = exc
        if off_outcome == "ok":
            off_outcome = "unknown"
    except DeviceError as exc:
        phase_error = exc
    except BaseException as exc:
        phase_error = exc
    finally:
        if isinstance(phase_error, TransportError):
            on_report = await _restore_on_connection_lost(backend, port, name, on_form)
        else:
            on_report = await _restore_on(backend, port, name, on_form, before_view)

    clean = off_outcome == "ok" and phase_error is None and cancel is None
    if clean and off_took:
        return _finish(settings, port, name, off_at, now, before_view, on_report)
    if clean and not off_took:
        # The off-verify read cleanly showed the port still enabled: the OFF never
        # took, so there was no outage. The restore's ON is a harmless no-op.
        raise WriteVerifyFailed(
            f"port {port} ({name}) PoE did not turn off; the cycle was aborted before any outage",
            before=before_view,
            after=off_view or before_view,
        )
    if cancel is not None:
        # Restore has been attempted; now honour cooperative cancellation.
        raise cancel
    raise _incomplete(port, name, off_outcome, on_report)


def _finish(
    settings: SwitchSettings,
    port: int,
    name: str | None,
    off_at: float | None,
    now: Any,
    before_view: dict[str, Any],
    on_report: _OnReport,
) -> dict[str, Any]:
    """Assemble the success envelope for the clean path, or raise if the ON failed."""
    if on_report.on_outcome != "ok":
        raise _incomplete(port, name, "ok", on_report)
    if not on_report.powered:
        raise PowerNotRestored(
            f"port {port} ({name}): PoE is ON but the device drew no power within "
            f"{settings.cycle_power_timeout_s}s",
            context={"port": port, "name": name, "poe": on_report.after},
        )
    on_at = on_report.on_at if on_report.on_at is not None else now()
    last = on_report.last
    return {
        "port": port,
        "name": name,
        "outcome": "cycled",
        "off_for_s": round(on_at - off_at, 3) if off_at is not None else None,
        "off_at": off_at,
        "on_at": on_at,
        "powered_at": now(),
        "power_w": last.power_w if last else None,
        "pd_class": last.pd_class if last else None,
        "before": before_view,
        "after": on_report.after,
    }


def _incomplete(
    port: int, name: str | None, off_outcome: str, on_report: _OnReport | None
) -> CycleIncomplete:
    report = on_report or _OnReport(on_outcome="not_attempted")
    may_be_unpowered = report.port_state_after != "on"
    return CycleIncomplete(
        f"port {port} ({name}) may be UNPOWERED: the cycle could not be completed after the "
        f"port was turned off (off step: {off_outcome}, on step: {report.on_outcome})",
        context={
            "port": port,
            "name": name,
            "off_outcome": off_outcome,
            "on_outcome": report.on_outcome,
            "error": report.error,
            "port_state_after": report.port_state_after,
            "may_be_unpowered": may_be_unpowered,
            "after": report.after,
        },
    )


class _OnReport:
    """The outcome of a single best-effort ON (restore) attempt. Never raised."""

    __slots__ = ("after", "error", "last", "on_at", "on_outcome", "port_state_after", "powered")

    def __init__(
        self,
        *,
        on_outcome: str,
        error: str | None = None,
        powered: bool = False,
        after: dict[str, Any] | None = None,
        last: PoePort | None = None,
        on_at: float | None = None,
        port_state_after: str = "unknown",
    ) -> None:
        self.on_outcome = on_outcome
        self.error = error
        self.powered = powered
        self.after = after
        self.last = last
        self.on_at = on_at
        self.port_state_after = port_state_after


async def _restore_on(
    backend: EasySmartSwitchBackend,
    port: int,
    name: str | None,
    on_form: RequestPlan,
    before_view: dict[str, Any],
) -> _OnReport:
    """Drive the port back ON and report the outcome. Never raises.

    The read/verify inside ``client.poe()`` carries the client's own single
    re-login allowance (an evicted session is recovered exactly once). Every
    error is captured so the caller can report it; a final re-read settles the
    port's real state for ``port_state_after``.
    """
    settings = backend.settings
    try:
        after = await _turn_on_with_retry(backend, port, name, on_form, before_view)
    except CycleIncomplete as exc:
        details = exc.details()
        return _OnReport(
            on_outcome="failed",
            error="ON_NOT_CONFIRMED",
            after=details.get("after"),
            port_state_after=await _settle_state(backend, port),
        )
    except OutcomeUnknown:
        state = await _settle_state(backend, port)
        return _OnReport(
            on_outcome="unknown",
            error="OUTCOME_UNKNOWN",
            after=await _safe_view(backend, port),
            port_state_after=state,
        )
    except DeviceError as exc:
        return _OnReport(
            on_outcome="failed",
            error=getattr(exc, "kind", "DEVICE_ERROR"),
            after=await _safe_view(backend, port),
            port_state_after=await _settle_state(backend, port),
        )
    except Exception:
        log.warning("best-effort re-enable raised an unexpected error", exc_info=True)
        return _OnReport(
            on_outcome="failed",
            error="INTERNAL_ERROR",
            port_state_after=await _settle_state(backend, port),
        )
    on_at = backend.now()
    powered, last = await _poll_power_safe(backend, port, on_at)
    return _OnReport(
        on_outcome="ok",
        powered=powered,
        after=poe_view(settings, after),
        last=last,
        on_at=on_at,
        port_state_after="on" if after.enabled else "off",
    )


async def _restore_on_connection_lost(
    backend: EasySmartSwitchBackend, port: int, name: str | None, on_form: RequestPlan
) -> _OnReport:
    """Restore ON after the connection to the switch dropped. Never raises.

    A transport failure means the session is gone. We refuse to silently re-login
    (that could spend a login attempt against a possibly-locked switch — the
    lockout-safety rule), so we end the broken session and make exactly one
    best-effort unauthenticated ON POST. An unreachable or logged-out switch will
    not apply it, leaving the port off; a final re-read settles the real state so
    the envelope can report ``may_be_unpowered`` honestly.
    """
    client = backend.client
    with contextlib.suppress(Exception):
        await client.logout()
    on_outcome = "attempted"
    try:
        await client.raw_post(on_form.path, on_form.fields)
    except Exception:
        on_outcome = "failed"
    return _OnReport(
        on_outcome=on_outcome,
        error="CONNECTION_LOST",
        after=await _safe_view(backend, port),
        port_state_after=await _settle_state(backend, port),
    )


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


async def _poll_power_safe(
    backend: EasySmartSwitchBackend, port: int, start_t: float
) -> tuple[bool, PoePort | None]:
    try:
        return await _poll_power(backend, port, start_t)
    except Exception:
        return False, None


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


async def _settle_state(backend: EasySmartSwitchBackend, port: int) -> str:
    """Re-read once to settle the port's real admin state; 'unknown' if unreadable."""
    try:
        state = (await backend.client.poe()).ports[port - 1]
    except Exception:
        return "unknown"
    return "on" if state.enabled else "off"


async def _safe_view(backend: EasySmartSwitchBackend, port: int) -> dict[str, Any] | None:
    try:
        state = (await backend.client.poe()).ports[port - 1]
    except Exception:
        return None
    return poe_view(backend.settings, state)

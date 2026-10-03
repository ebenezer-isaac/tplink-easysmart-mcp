"""F2 — cancellation (and any non-DeviceError) between OFF and ON skips the restore.

``_run_cycle`` guards the restore with ``except DeviceError`` only, and
``run_tool`` wraps tools in ``except Exception``. ``asyncio.CancelledError`` is a
``BaseException`` (not ``Exception``/``DeviceError``), so a cancellation during
the off window:

* is NOT caught by ``_run_cycle`` → ``_best_effort_on`` never runs → the port
  stays OFF (camera dark);
* is NOT caught by ``run_tool`` → it propagates straight out of the tool, so the
  "every tool returns an envelope / never raises" guarantee is also broken.

The claim lists "cancellation" among the paths after which the tool "ALWAYS
attempts to turn it back on". It does not.

(Whether swallowing a cancel to restore power is the *right* fix is a judgement
call; this test only shows the claim's stated behaviour does not happen.)
"""

from __future__ import annotations

import asyncio
import contextlib

from tests.switch_fakes import FakeClock, StatefulSwitch, write_backend
from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.switch.cycle import poe_cycle_op

POE_CGI = "/poe_port_config.cgi"


class CancelDuringOffWindow(FakeClock):
    """Raises ``CancelledError`` on the off-window sleep (after the port is OFF)."""

    async def sleep(self, seconds: float) -> None:
        raise asyncio.CancelledError()


class BugDuringOffWindow(FakeClock):
    """Raises a plain (non-Device) ``RuntimeError`` on the off-window sleep."""

    async def sleep(self, seconds: float) -> None:
        raise RuntimeError("an unexpected non-DeviceError in the off window")


async def test_cancellation_between_off_and_on_still_restores_power(tmp_path) -> None:
    # CLAIMED INVARIANT: after the port is off, EVERY failure path (incl. cancellation)
    # attempts to turn it back on. FAILS today: CancelledError escapes the restore guard.
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, clock=CancelDuringOffWindow(), port_map="cam1=1")

    # The tool should restore power before unwinding; today the cancel escapes instead.
    with contextlib.suppress(asyncio.CancelledError):
        await run_tool(
            "switch_poe_cycle", lambda: poe_cycle_op(backend, port_or_name="cam1", off_seconds=10)
        )

    on_writes = [b for b in switch.poe_write_bodies() if b.get("name_pstate") == "2"]
    assert on_writes != [], "a restore ('on') write must be attempted even on cancellation"
    assert switch.poe["state"][0] == 1, "the camera must not be left dark by a cancel"


async def test_non_device_exception_between_off_and_on_still_restores_power(tmp_path) -> None:
    # CLAIMED INVARIANT: "on every failure path (exception, ...)" the port is driven on.
    # FAILS today: a non-DeviceError skips _run_cycle's `except DeviceError` restore.
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, clock=BugDuringOffWindow(), port_map="cam1=1")

    env = await run_tool(
        "switch_poe_cycle", lambda: poe_cycle_op(backend, port_or_name="cam1", off_seconds=10)
    )

    on_writes = [b for b in switch.poe_write_bodies() if b.get("name_pstate") == "2"]
    assert on_writes != [], "a restore ('on') write must be attempted after any exception"
    # Must report the UNPOWERED risk, not a bare INTERNAL_ERROR.
    assert env["error"]["code"] == "CYCLE_INCOMPLETE"

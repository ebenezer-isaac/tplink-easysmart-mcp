"""TC-F4 (closed by X1b) - a bad timestamp can no longer wedge a port forever.

The old ``CycleMarker`` refused while ``age = now() - started_at < 300`` with no
clamp, so a future-dated ``started_at`` (corrupt/tampered file, NTP step, or a
backward wall-clock jump after a crash) made ``age`` permanently negative and every
cycle was refused forever.

The X1b ``CycleGuard`` treats a reservation as blocking only while its age is within
``[0, stale_after_s)``; a negative age (future/backward-clock) or an age past the
window is stale, so the port recovers instead of self-wedging. (Previously xfail:
"deferred to X1b ReservationStore" - now implemented, so these must PASS.)
"""

from __future__ import annotations

import json

from tests.switch_fakes import FakeClock, StatefulSwitch, write_backend
from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.switch.cycle import poe_cycle_op

POE_CGI = "/poe_port_config.cgi"


async def _cycle(backend, **kwargs) -> dict:
    return await run_tool("switch_poe_cycle", lambda: poe_cycle_op(backend, **kwargs))


async def test_future_timestamp_reservation_does_not_wedge_every_cycle(tmp_path) -> None:
    switch = StatefulSwitch()
    clock = FakeClock(start=1000.0)
    backend, _ = write_backend(tmp_path, switch, clock=clock, port_map="cam1=1")

    # A leftover reservation with a nonsensical future reserved_at (corrupt / NTP step).
    path = backend.cycle_guard.path(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "key": path.stem,
                "epoch": 0,
                "reservations": {
                    "stale-holder": {
                        "reserved_at": 10_000_000.0,
                        "meta": {"port": 1, "name": "cam1"},
                    }
                },
                "last_update": 10_000_000.0,
            }
        ),
        encoding="utf-8",
    )

    env = await _cycle(backend, port_or_name="cam1", off_seconds=10)

    # A future stamp reads as stale (negative age), so the cycle proceeds, not wedges.
    assert env["success"] is True, f"a future-dated reservation must not block forever; {env}"
    assert switch.count("POST", POE_CGI) == 2


async def test_backward_clock_jump_does_not_wedge_an_existing_reservation(tmp_path) -> None:
    switch = StatefulSwitch()
    clock = FakeClock(start=5000.0)
    backend, _ = write_backend(tmp_path, switch, clock=clock, port_map="cam1=1")

    # A cycle reserved (stamped) at t=5000 and then crashed (reservation unreleased)...
    backend.cycle_guard.reserve(1, "cam1")
    clock.t = 1000.0  # ...then the wall clock jumps backward (NTP / DST / manual).

    env = await _cycle(backend, port_or_name="cam1", off_seconds=10)

    # The leftover reservation now reads as a negative age => stale => recover, not wedge.
    assert env["success"] is True, f"a backward clock jump must not wedge the port; {env}"

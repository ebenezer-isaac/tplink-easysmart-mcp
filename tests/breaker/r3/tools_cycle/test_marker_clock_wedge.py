"""F4 — a future-dated marker / backward clock jump wedges switch_poe_cycle.

``CycleMarker.begin`` computes ``age = now() - started_at`` and refuses while
``age < MARKER_STALE_AFTER_S`` (300 s). A ``started_at`` in the future makes
``age`` permanently negative, so ``age < 300`` is always true and *every* cycle
is refused as ``CYCLE_IN_PROGRESS`` until wall-clock time passes the stamp (or a
human deletes the file). A future stamp arises from a corrupt/tampered marker, a
leftover marker plus an NTP step, or a backward wall-clock jump after a crash.

Note the asymmetry: an *unreadable or non-dict* marker fails OPEN (``_read``
returns ``{"started_at": None}`` → treated as stale → the cycle proceeds), but a
structurally-valid marker with a bad numeric ``started_at`` fails CLOSED and can
never clear itself.
"""

from __future__ import annotations

from tests.switch_fakes import FakeClock, StatefulSwitch, write_backend
from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.switch.cycle import CycleMarker, poe_cycle_op

POE_CGI = "/poe_port_config.cgi"


async def _cycle(backend, **kwargs) -> dict:
    return await run_tool("switch_poe_cycle", lambda: poe_cycle_op(backend, **kwargs))


async def test_future_timestamp_marker_wedges_every_cycle(tmp_path) -> None:
    switch = StatefulSwitch()
    clock = FakeClock(start=1000.0)
    backend, _ = write_backend(tmp_path, switch, clock=clock, port_map="cam1=1")

    marker = CycleMarker(backend.settings.state_path, backend.settings.host, now=clock.now)
    marker._write({"started_at": 10_000_000.0, "port": 1, "name": "cam1"})

    env = await _cycle(backend, port_or_name="cam1", off_seconds=10)

    # CLAIMED INVARIANT: a marker is honoured only while it is plausibly fresh; a
    # nonsensical (future) stamp must be treated like a corrupt one (stale -> proceed),
    # not wedge the tool forever. FAILS today: negative age < 300 => CYCLE_IN_PROGRESS.
    assert env["success"] is True, (
        f"a future-dated marker must not block cycles forever; got {env.get('error')}"
    )
    assert switch.count("POST", POE_CGI) == 2


async def test_backward_clock_jump_wedges_an_existing_marker(tmp_path) -> None:
    switch = StatefulSwitch()
    clock = FakeClock(start=5000.0)
    backend, _ = write_backend(tmp_path, switch, clock=clock, port_map="cam1=1")

    # A cycle started (marker stamped) at t=5000, then the wall clock jumps back.
    marker = CycleMarker(backend.settings.state_path, backend.settings.host, now=clock.now)
    marker.begin(1, "cam1")
    clock.t = 1000.0  # NTP / DST / manual correction steps the clock backwards

    env = await _cycle(backend, port_or_name="cam1", off_seconds=10)

    # CLAIMED INVARIANT: a backward wall-clock jump must not permanently block the
    # cycle tool. FAILS today: the leftover marker now reads as negative-age => fresh.
    assert env["success"] is True, (
        f"a backward clock jump must not wedge switch_poe_cycle; got {env.get('error')}"
    )

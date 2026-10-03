"""switch_poe_cycle state machine: fake clock / fake sleep, no real waiting."""

from __future__ import annotations

import json

from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.switch.cycle import CycleMarker, poe_cycle_op

from .switch_fakes import FakeClock, StatefulSwitch, write_backend

POE_CGI = "/poe_port_config.cgi"
LOGOUT = "/Logout.htm"


async def cyc(backend, **kwargs) -> dict:
    """Run the cycle op through the tool wrapper and return its envelope."""
    return await run_tool("switch_poe_cycle", lambda: poe_cycle_op(backend, **kwargs))


class EvictClock(FakeClock):
    """A fake clock that evicts the switch's session on the first sleep (the off window)."""

    def __init__(self, switch: StatefulSwitch, *, break_login: bool = False) -> None:
        super().__init__()
        self._switch = switch
        self._break_login = break_login
        self._fired = False

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        if not self._fired:
            self._fired = True
            self._switch.evict()
            if self._break_login:
                self._switch.login_errtype = 1  # the re-login will now fail (breaker trips)


# ---- happy path -------------------------------------------------------------


async def test_happy_path_two_posts_identical_priority_limit(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, clock = write_backend(tmp_path, switch, port_map="cam1=1;uplink=16")
    env = await cyc(backend, port_or_name="cam1", off_seconds=10)
    assert env["success"] is True
    data = env["data"]
    assert data["outcome"] == "cycled"
    assert {"port", "name", "before", "after", "off_for_s", "outcome"} <= data.keys()
    assert data["port"] == 1 and data["name"] == "cam1"
    assert data["off_for_s"] == 10
    assert data["power_w"] > 0
    bodies = switch.poe_write_bodies()
    assert len(bodies) == 2  # exactly off + on
    assert bodies[0]["name_ppriority"] == bodies[1]["name_ppriority"]
    assert bodies[0]["name_ppowerlimit"] == bodies[1]["name_ppowerlimit"]
    assert clock.sleeps == [10]  # only the off window; power was back immediately
    assert switch.count("GET", LOGOUT) == 1


# ---- guards -----------------------------------------------------------------


async def test_already_off_is_refused_with_zero_writes(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch)
    env = await cyc(backend, port_or_name=8, off_seconds=10)  # port 8 seeded off
    assert env["error"]["code"] == "ALREADY_OFF"
    assert switch.count("POST", POE_CGI) == 0


async def test_non_poe_and_protected_refused_with_no_request(tmp_path) -> None:
    switch = StatefulSwitch()
    # Protect PoE port 2 (plus the uplink) so protection is tested on a real PoE port.
    backend, _ = write_backend(tmp_path, switch, protected_ports="2,16")
    non_poe = await cyc(backend, port_or_name=9, off_seconds=10)
    assert non_poe["error"]["code"] == "NOT_POE_PORT"
    protected = await cyc(backend, port_or_name=2, off_seconds=10)
    assert protected["error"]["code"] == "PROTECTED_PORT"
    assert switch.calls == []


async def test_concurrent_cycle_is_refused_with_no_requests(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch)
    async with backend.cycle_lock:  # simulate a cycle already in flight
        env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["error"]["code"] == "CYCLE_IN_PROGRESS"
    assert switch.calls == []


async def test_fresh_marker_refuses_stale_marker_allows(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, clock = write_backend(tmp_path, switch)
    marker = CycleMarker(backend.settings.state_path, backend.settings.host, now=clock.now)
    # A fresh marker (just written) blocks a new cycle.
    marker.begin(1, "cam1")
    blocked = await cyc(backend, port_or_name=1, off_seconds=10)
    assert blocked["error"]["code"] == "CYCLE_IN_PROGRESS"
    # Advance past the stale window: the marker is now ignored and the cycle runs.
    clock.t += 301
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["success"] is True


# ---- off_seconds bounds -----------------------------------------------------


async def test_off_seconds_out_of_bounds_is_invalid_argument_no_network(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch)
    for bad in [0, 4, 121, float("nan"), -1, "ten"]:
        env = await cyc(backend, port_or_name=1, off_seconds=bad)
        assert env["error"]["code"] == "INVALID_ARGUMENT"
    assert switch.calls == []


# ---- on-step retry / incompletion -------------------------------------------


async def test_on_swallowed_once_then_retry_succeeds(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.ignore_poe_on = 1  # first "on" write is silently dropped
    backend, _ = write_backend(tmp_path, switch)
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["success"] is True
    assert env["data"]["after"]["enabled"] is True
    # off + on(swallowed) + on(retry) = 3 writes
    assert switch.count("POST", POE_CGI) == 3


async def test_on_fails_twice_is_cycle_incomplete_unpowered(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.ignore_poe_on = 2  # both on-attempts dropped
    backend, _ = write_backend(tmp_path, switch)
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["error"]["code"] == "CYCLE_INCOMPLETE"
    assert "UNPOWERED" in env["error"]["message"]
    assert switch.count("GET", LOGOUT) == 1


# ---- eviction / breaker during the off window -------------------------------


async def test_eviction_during_sleep_relogins_once_then_completes(tmp_path) -> None:
    switch = StatefulSwitch()
    clock = EvictClock(switch)
    backend, _ = write_backend(tmp_path, switch, clock=clock)
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["success"] is True
    assert switch.logins == 2  # initial login + exactly one re-login


async def test_breaker_trip_on_relogin_is_incomplete_no_further_login(tmp_path) -> None:
    switch = StatefulSwitch()
    clock = EvictClock(switch, break_login=True)
    backend, _ = write_backend(tmp_path, switch, clock=clock)
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["error"]["code"] == "CYCLE_INCOMPLETE"
    assert env["error"]["details"]["error"] == "AUTH_FAILED"
    assert switch.logins == 2  # initial + the failed re-login; the breaker blocks any more
    assert backend.breaker.is_open is True


# ---- power never returns -----------------------------------------------------


async def test_power_not_restored_after_timeout(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.power_restores = False  # PoE turns on but the PD draws nothing
    backend, _ = write_backend(tmp_path, switch)
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert env["error"]["code"] == "POWER_NOT_RESTORED"
    assert switch.count("GET", LOGOUT) == 1


# ---- dry-run ----------------------------------------------------------------


async def test_dry_run_returns_both_forms_and_sends_no_write(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, dry_run="true", port_map="cam1=1")
    env = await cyc(backend, port_or_name="cam1", off_seconds=15)
    assert env["success"] is True
    assert env["data"]["dry_run"] is True
    assert env["data"]["planned"]["off"]["name_pstate"] == "1"
    assert env["data"]["planned"]["on"]["name_pstate"] == "2"
    assert env["data"]["planned"]["sleep_s"] == 15
    assert switch.count("POST", POE_CGI) == 0


async def test_cycle_envelope_has_no_password(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, password="TopSecret99")
    env = await cyc(backend, port_or_name=1, off_seconds=10)
    assert "TopSecret99" not in json.dumps(env)

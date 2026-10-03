"""F1 — the OFF window sits OUTSIDE the ON-restore guard.

``_run_cycle`` turns the port off and *verifies* the off BEFORE the
``try/except`` that is supposed to guarantee a restore::

    await client.submit(off_form)                      # (A) can reset after landing
    off_state = (await client.poe()).ports[port - 1]   # (B) can reset / evict / malform
    if off_state.enabled: raise WriteVerifyFailed(...)
    off_at = now()
    try:
        ... sleep / turn ON / poll ...
    except DeviceError:
        _best_effort_on(...)   # <- only reached from inside the try

So any device error at (A-after-landing) or (B) leaves the port OFF and raises
straight out — no ON attempt, and the envelope is a bare TRANSPORT_ERROR /
OUTCOME_UNKNOWN, never ``CYCLE_INCOMPLETE ... may be UNPOWERED``.

This disproves the claim's "after turning a port off it ALWAYS attempts to turn
it back on, on every failure path (exception ... eviction, timeout ...)".
"""

from __future__ import annotations

from urllib.parse import parse_qsl

import httpx

from tests.switch_fakes import StatefulSwitch, write_backend
from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.switch.cycle import poe_cycle_op

POE_CGI = "/poe_port_config.cgi"
POE_PAGE = "/PoeConfigRpm.htm"


async def _cycle(backend, **kwargs) -> dict:
    return await run_tool("switch_poe_cycle", lambda: poe_cycle_op(backend, **kwargs))


def _is_off_write(request: httpx.Request) -> bool:
    if request.method != "POST" or request.url.path != POE_CGI:
        return False
    fields = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
    return fields.get("name_pstate") == "1"  # PSTATE_DISABLE


class ResetOnOffVerifyRead(StatefulSwitch):
    """The off write lands; the very next PoE read (the off-verify) resets."""

    def __init__(self) -> None:
        super().__init__()
        self._arm_read_reset = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self._arm_read_reset and request.url.path == POE_PAGE:
            self._arm_read_reset = False
            self.calls.append(request)
            raise httpx.ConnectError("switch reset the connection on the read")
        return super()._handle(request)

    def _write_poe(self, request: httpx.Request) -> httpx.Response:
        response = super()._write_poe(request)  # the off is applied here
        if _is_off_write(request):
            self._arm_read_reset = True
        return response


class ResetAfterOffPostLands(StatefulSwitch):
    """The off POST is applied, then the connection is reset (reset-after-landing)."""

    def __init__(self) -> None:
        super().__init__()
        self._done = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not self._done and _is_off_write(request):
            self._done = True
            self.calls.append(request)
            if self.device_authed:
                self._write_poe(request)  # land the OFF on the device
            raise httpx.ConnectError("reset after the off POST landed")
        return super()._handle(request)


async def test_reset_on_off_verify_read_still_attempts_restore(tmp_path) -> None:
    # CLAIMED INVARIANT: once the port has been turned off, every failure path still
    # attempts to turn it back on and reports CYCLE_INCOMPLETE "may be UNPOWERED".
    # FAILS today: the off-verify read error escapes before the restore guard.
    switch = ResetOnOffVerifyRead()
    backend, _ = write_backend(tmp_path, switch, port_map="cam1=1")

    env = await _cycle(backend, port_or_name="cam1", off_seconds=10)

    assert switch.poe["state"][0] == 0, "the off write landed; the camera is dark"
    on_writes = [b for b in switch.poe_write_bodies() if b.get("name_pstate") == "2"]
    assert on_writes != [], "a restore ('on') write must be attempted after the port went off"
    assert env["error"]["code"] == "CYCLE_INCOMPLETE"
    assert "UNPOWERED" in env["error"]["message"]


async def test_reset_after_off_post_lands_is_settled_by_reread_and_restore(tmp_path) -> None:
    # CLAIMED INVARIANT: a connection reset after a POST yields OUTCOME_UNKNOWN that is
    # settled by RE-READING (never resending) and the port is still driven back on.
    # FAILS today: the cycle aborts with OUTCOME_UNKNOWN and never restores power.
    switch = ResetAfterOffPostLands()
    backend, _ = write_backend(tmp_path, switch, port_map="cam1=1")

    env = await _cycle(backend, port_or_name="cam1", off_seconds=10)

    on_writes = [b for b in switch.poe_write_bodies() if b.get("name_pstate") == "2"]
    assert on_writes != [], "after settling the OUTCOME_UNKNOWN off, an on write must follow"
    assert switch.poe["state"][0] == 1, "the camera must end powered back on, not dark"
    assert env["error"] is None or env["error"]["code"] == "CYCLE_INCOMPLETE"

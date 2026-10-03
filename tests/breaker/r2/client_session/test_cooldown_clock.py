"""R2 / clocks + durability — the login cooldown is unbounded and wall-clock-based.

The claim says the client "persist[s] a 5-minute cooldown for 3/4/5 across
processes" and that the breaker/cooldown are "fail-closed on corruption". These
tests attack the *time* arithmetic behind that cooldown.

``LoginCooldown.remaining()`` computes ``seconds - (now - last_failure_at)`` with
no upper clamp, and reads ``now`` from the wall clock (``time.time``). So:

* a ``last_failure_at`` that lies in the *future* — produced by a backward
  wall-clock step (NTP correction, a VM/RTC-less host booting then syncing, a
  manual clock change) or by any numeric-but-garbage value that survives the
  "corrupt ⇒ fail closed" guard — yields a cooldown far longer than the
  configured window, up to the size of the clock jump;
* the only documented operator recovery, ``tplink-easysmart-mcp breaker --clear``,
  clears the breaker file and never touches the cooldown file, so the lockout has
  no supported way out short of hand-deleting the state file.

Each test asserts the safe behaviour (a cooldown can never exceed its configured
length, and the documented recovery works) and fails against the current code.
No real device is contacted.
"""

from __future__ import annotations

import json

import pytest

from tests.switch_fakes import FakeSwitch, fixture, make_settings
from tplink_easysmart_mcp.core.breaker import LoginBreaker
from tplink_easysmart_mcp.switch.auth import LoginCooldown
from tplink_easysmart_mcp.switch.client import SwitchClient
from tplink_easysmart_mcp.switch.errors import CooldownActive

DEVICE = "192.0.2.10"
WINDOW = 300
ROOT, LOGON, SYSINFO = "/", "/logon.cgi", "/SystemInfoRpm.htm"


def _good_login_fake() -> FakeSwitch:
    return (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
    )


def test_future_timestamp_is_clamped_to_the_window(tmp_path) -> None:
    """A cooldown can never last longer than its configured length (300 s here)."""
    now = 1000.0
    cooldown = LoginCooldown(tmp_path, DEVICE, WINDOW, now=lambda: now)
    # A valid-JSON, numeric timestamp in the future (clock stepped back, or a
    # garbage write) is *not* treated as corrupt, so the fail-closed guard
    # accepts it verbatim.
    cooldown.path.write_text(
        json.dumps({"last_failure_at": now + 1_000_000}), encoding="utf-8"
    )
    # Correct: remaining is bounded by the configured window.
    assert cooldown.remaining() <= WINDOW


def test_backward_wall_clock_step_does_not_extend_cooldown(tmp_path) -> None:
    """An NTP/manual backward step after a real failure must not lengthen the wait."""
    clock = {"t": 2_000_000.0}
    cooldown = LoginCooldown(tmp_path, DEVICE, WINDOW, now=lambda: clock["t"])
    cooldown.record()  # a genuine soft failure (errType 3/4/5) at t = 2_000_000
    clock["t"] -= 3_600  # the wall clock is corrected back by one hour
    # A real soft failure justifies at most a 300 s wait, never ~3900 s.
    assert cooldown.remaining() <= WINDOW


async def test_breaker_clear_recovers_a_future_timestamp_lockout(tmp_path) -> None:
    """The documented recovery must free a login blocked by a bad cooldown timestamp."""
    now = 5000.0
    settings = make_settings(tmp_path)
    breaker = LoginBreaker(tmp_path, settings.host)
    cooldown = LoginCooldown(tmp_path, settings.host, settings.login_cooldown_s, now=lambda: now)
    cooldown.path.write_text(
        json.dumps({"last_failure_at": now + 10_000_000}), encoding="utf-8"
    )
    fake = _good_login_fake()
    client = SwitchClient(settings, breaker, cooldown, transport=fake.transport())

    # The only documented operator action for a stuck login.
    breaker.clear()

    with pytest.raises(CooldownActive) as info:
        await client.login()
    assert fake.calls == []  # gated before any network, as designed
    # The reported wait must not exceed the configured window — and the documented
    # recovery should have cleared it. Today it reports ~10_000_000 s and there is
    # no supported way to clear the cooldown.
    assert info.value.details()["retry_after_s"] <= settings.login_cooldown_s

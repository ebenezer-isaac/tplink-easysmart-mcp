"""State-machine and adversarial tests for the switch authenticator.

Everything runs against an in-process fake switch; no real device is contacted.
The central claim under attack: the client makes at most one POST /logon.cgi per
explicit login, never auto-retries a failed login, and never POSTs into restored
or encrypted modes.

Post X1b core sync: the breaker + the (deleted) ``LoginCooldown`` are one canonical
``LoginBreaker`` reservation. errType 1/2/6/unknown are ``FAILURE`` (trip at budget,
raising ``BreakerOpen`` thereafter); 3/4/5 are ``BUSY`` cooldowns (raising
``Cooldown``); a restored-account / encrypted-variant / session-busy ``GET /`` sends
no credential, so the reservation is ``ABORT`` and the budget is NOT spent (the old
code tripped the breaker on restored/encrypted — that penalised a refusal in which
no credential ever left the process; ABORT is the corrected behaviour).
"""

from __future__ import annotations

import time

import httpx
import pytest

from tplink_easysmart_mcp.core.breaker import LoginBreaker, canonical_device_key
from tplink_easysmart_mcp.core.errors import BreakerOpen, Cooldown, LoginDisabled
from tplink_easysmart_mcp.core.state import Outcome
from tplink_easysmart_mcp.switch.backend import EasySmartSwitchBackend
from tplink_easysmart_mcp.switch.errors import (
    AuthFailed,
    AuthVariantUnsupported,
    LockedOut,
    LoginNotAccepted,
    RestoredAccountMode,
    SessionBusy,
    SessionsFull,
)

from .switch_fakes import FakeSwitch, fixture, login_page_with_cookie, make_settings

LOGON = "/logon.cgi"
ROOT = "/"
SYSINFO = "/SystemInfoRpm.htm"


def build(tmp_path, fake, *, now=time.time, **overrides):
    """A client + its breaker, wired exactly as production builds them."""
    settings = make_settings(tmp_path, **overrides)
    backend = EasySmartSwitchBackend(settings, transport=fake.transport(), now=now)
    return backend.client, backend.breaker


def _good_login_fake() -> FakeSwitch:
    return (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
    )


# ---- restored-account guard --------------------------------------------------


async def test_restored_account_page_makes_zero_posts(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("login_page_restored_account.html"))
    client, breaker = build(tmp_path, fake)
    with pytest.raises(RestoredAccountMode):
        await client.login()
    assert fake.count("POST", LOGON) == 0
    # No credential was sent, so the attempt is ABORTed and the budget is not spent.
    assert breaker.status()["state"] == "closed"


async def test_errtype6_post_response_sends_nothing_further(tmp_path) -> None:
    fake = (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype6_password_change.html"))
    )
    client, breaker = build(tmp_path, fake)
    with pytest.raises(RestoredAccountMode):
        await client.login()
    assert fake.count("POST", LOGON) == 1
    assert fake.count("GET", SYSINFO) == 0  # no confirm after an errType-6 POST
    assert breaker.status()["state"] == "open"  # a POST landed → FAILURE trips the breaker


async def test_encrypted_variant_makes_zero_posts(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("login_page_encrypted_variant.html"))
    client, breaker = build(tmp_path, fake)
    with pytest.raises(AuthVariantUnsupported):
        await client.login()
    assert fake.count("POST", LOGON) == 0
    assert breaker.status()["state"] == "closed"  # no credential sent → ABORT, budget intact


# ---- breaker-tripping errTypes ----------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "exc"),
    [
        ("logon_response_errtype1_bad_credentials.html", AuthFailed),
        ("logon_response_errtype2_user_blocked.html", LockedOut),
    ],
)
async def test_errtype_1_2_trip_breaker_and_block_next_login(tmp_path, fixture_name, exc) -> None:
    fake = (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture(fixture_name))
    )
    client, breaker = build(tmp_path, fake)
    with pytest.raises(exc):
        await client.login()
    assert fake.count("POST", LOGON) == 1
    assert breaker.status()["state"] == "open"

    calls_before = len(fake.calls)
    with pytest.raises(BreakerOpen):
        await client.login()  # breaker open ⇒ no network at all
    assert len(fake.calls) == calls_before


# ---- soft failures: cooldown, not breaker -----------------------------------


async def test_errtype4_records_cooldown_then_allows_after_expiry(tmp_path) -> None:
    clock = {"t": 1000.0}
    fake = (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route(
            "POST",
            LOGON,
            fixture("logon_response_errtype4_sessions_full.html"),
            fixture("logon_response_errtype0.html"),
        )
        .route("GET", SYSINFO, fixture("system_info.html"))
    )
    client, breaker = build(tmp_path, fake, now=lambda: clock["t"])

    with pytest.raises(SessionsFull):
        await client.login()
    status = breaker.status()
    assert status["state"] == "cooldown"  # a soft busy, not a budget failure
    assert status["cooldown_remaining_s"] == 300

    calls_before = len(fake.calls)
    with pytest.raises(Cooldown) as info:
        await client.login()  # within the window ⇒ no network
    assert len(fake.calls) == calls_before
    assert info.value.details()["cooldown_remaining_s"] == 300

    clock["t"] += 300  # frozen clock advances past the cooldown
    result = await client.login()
    assert result.system_info.hardware == "TL-SG1016PE 3.0"
    assert breaker.status()["cooldown_remaining_s"] == 0  # cleared on confirmed login


# ---- errType-0 ambiguity (reference issue #49) ------------------------------


async def test_errtype0_confirm_login_page_is_login_not_accepted(tmp_path) -> None:
    fake = (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("login_page.html"))  # confirm shows the login page
    )
    client, breaker = build(tmp_path, fake)
    with pytest.raises(LoginNotAccepted):
        await client.login()
    # A credential WAS sent and silently ignored (reference issue #49): fail closed as
    # a FAILURE so the budget is spent and the breaker does not retry blindly.
    assert breaker.status()["state"] == "open"


async def test_errtype0_confirm_data_succeeds(tmp_path) -> None:
    client, _ = build(tmp_path, _good_login_fake())
    result = await client.login()
    assert result.system_info.hw_revision == "3.0"
    assert client.authenticated is True


# ---- connection reset on the POST -------------------------------------------


async def test_reset_on_post_confirms_without_resending(tmp_path) -> None:
    fake = (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, httpx.RemoteProtocolError("Server disconnected"))
        .route("GET", SYSINFO, fixture("system_info.html"))
    )
    client, _ = build(tmp_path, fake)
    result = await client.login()
    assert fake.count("POST", LOGON) == 1  # never re-POSTed
    assert fake.count("GET", SYSINFO) == 1
    assert result.system_info.firmware.startswith("1.0.0")


# ---- login disabled ----------------------------------------------------------


async def test_login_disabled_makes_no_http(tmp_path) -> None:
    fake = _good_login_fake()
    client, _ = build(tmp_path, fake, login_disabled="true")
    with pytest.raises(LoginDisabled):
        await client.login()
    assert fake.calls == []


# ---- session model detection -------------------------------------------------


async def test_cookie_model_post_carries_cookie(tmp_path) -> None:
    fake = (
        FakeSwitch()
        .route("GET", ROOT, login_page_with_cookie())
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
    )
    client, _ = build(tmp_path, fake)
    result = await client.login()
    assert result.session_model.value == "cookie"
    post = fake.request_for("POST", LOGON)
    assert "H_P_SSID" in post.headers.get("cookie", "")


async def test_ip_bound_active_frameset_is_session_busy_no_post(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("system_info.html"))  # data, empty jar
    client, breaker = build(tmp_path, fake)
    with pytest.raises(SessionBusy):
        await client.login()
    assert fake.count("POST", LOGON) == 0
    assert breaker.status()["state"] == "closed"  # not our fault; ABORT, do not trip


# ---- persistence -------------------------------------------------------------


async def test_cooldown_persists_across_breaker_instances(tmp_path) -> None:
    clock = {"t": 5000.0}
    key = canonical_device_key("192.0.2.10")
    first = LoginBreaker(tmp_path, key, max_failures=5, clock=lambda: clock["t"])
    first.reserve_attempt().release(Outcome.BUSY, cooldown_s=300)
    # A fresh instance (the next process) on the same dir/key still sees the cooldown.
    second = LoginBreaker(tmp_path, key, max_failures=5, clock=lambda: clock["t"])
    assert second.status()["cooldown_remaining_s"] == 300
    with pytest.raises(Cooldown):
        second.reserve_attempt()

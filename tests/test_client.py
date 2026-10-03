"""Session/queue behaviour of SwitchClient against an in-process fake switch."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from tplink_easysmart_mcp.core.breaker import LoginBreaker, canonical_device_key
from tplink_easysmart_mcp.switch.client import SwitchClient
from tplink_easysmart_mcp.switch.errors import OutcomeUnknown, SessionLost
from tplink_easysmart_mcp.switch.forms import RequestPlan

from .switch_fakes import FakeSwitch, fixture, make_settings

ROOT, LOGON, SYSINFO = "/", "/logon.cgi", "/SystemInfoRpm.htm"
POE = "/PoeConfigRpm.htm"


def _breaker_for(tmp_path, settings) -> LoginBreaker:
    return LoginBreaker(
        tmp_path, canonical_device_key(settings.host), max_failures=settings.max_login_failures
    )


def build(tmp_path, fake, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return SwitchClient(settings, _breaker_for(tmp_path, settings), transport=fake.transport())


def _login_routes(fake: FakeSwitch) -> FakeSwitch:
    return (
        fake.route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
        .route("GET", "/Logout.htm", fixture("login_page.html"))
    )


async def test_lazy_login_on_first_read(tmp_path) -> None:
    fake = _login_routes(FakeSwitch()).route("GET", POE, fixture("poe_config.html"))
    client = build(tmp_path, fake)
    assert client.authenticated is False
    snap = await client.poe()
    assert client.authenticated is True
    assert fake.count("POST", LOGON) == 1
    assert snap.poe_port_num == 8


async def test_eviction_triggers_one_relogin_then_success(tmp_path) -> None:
    fake = _login_routes(FakeSwitch()).route(
        "GET", POE, fixture("poe_config_returned_login_page.html"), fixture("poe_config.html")
    )
    client = build(tmp_path, fake)
    snap = await client.poe()
    assert snap.poe_port_num == 8
    assert fake.count("POST", LOGON) == 2  # initial login + exactly one re-login


async def test_second_eviction_is_session_lost_with_two_logins(tmp_path) -> None:
    fake = _login_routes(FakeSwitch()).route(
        "GET",
        POE,
        fixture("poe_config_returned_login_page.html"),  # always the login page
    )
    client = build(tmp_path, fake)
    with pytest.raises(SessionLost):
        await client.poe()
    assert fake.count("POST", LOGON) == 2  # no third login attempt


async def test_concurrent_reads_are_serialised(tmp_path) -> None:
    active = 0
    observed_max = 0
    bodies = {
        ROOT: fixture("login_page.html"),
        LOGON: fixture("logon_response_errtype0.html"),
        SYSINFO: fixture("system_info.html"),
        POE: fixture("poe_config.html"),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, observed_max
        active += 1
        observed_max = max(observed_max, active)
        try:
            await asyncio.sleep(0)  # yield; an unserialised peer would enter here
            return httpx.Response(200, text=bodies.get(request.url.path, "nope"))
        finally:
            active -= 1

    settings = make_settings(tmp_path)
    client = SwitchClient(
        settings, _breaker_for(tmp_path, settings), transport=httpx.MockTransport(handler)
    )
    await asyncio.gather(client.poe(), client.poe())
    assert observed_max == 1


async def test_session_scope_logs_out_even_on_exception(tmp_path) -> None:
    fake = _login_routes(FakeSwitch())
    client = build(tmp_path, fake)
    await client.login()
    with pytest.raises(RuntimeError):
        async with client.session_scope(logout_after=True):
            raise RuntimeError("boom in the body")
    assert fake.count("GET", "/Logout.htm") == 1
    assert client.authenticated is False


async def test_logout_when_never_logged_in_makes_no_request(tmp_path) -> None:
    fake = _login_routes(FakeSwitch())
    client = build(tmp_path, fake)
    await client.logout()
    assert fake.calls == []


async def test_context_exit_logs_out(tmp_path) -> None:
    fake = _login_routes(FakeSwitch())
    client = build(tmp_path, fake)
    async with client:
        await client.login()
    assert fake.count("GET", "/Logout.htm") == 1


async def test_dry_run_submit_sends_nothing_and_redacts(tmp_path) -> None:
    fake = _login_routes(FakeSwitch())
    client = build(tmp_path, fake, allow_writes="true", protected_ports="16", dry_run="true")
    plan = RequestPlan("POST", LOGON, (("username", "admin"), ("password", "TestPass123")))
    returned = await client.submit(plan)
    assert fake.calls == []  # not even a login
    assert dict(returned.fields)["password"] == "<redacted>"


async def test_reset_on_submit_is_outcome_unknown(tmp_path) -> None:
    fake = _login_routes(FakeSwitch()).route(
        "POST", "/poe_port_config.cgi", httpx.ConnectError("reset")
    )
    client = build(tmp_path, fake, allow_writes="true", protected_ports="16")
    await client.login()
    plan = RequestPlan("POST", "/poe_port_config.cgi", (("sel_1", "1"), ("applay", "Apply")))
    with pytest.raises(OutcomeUnknown):
        await client.submit(plan)


async def test_logs_never_contain_password_or_cookie(tmp_path, caplog) -> None:
    cookie = "H_P_SSID=tplink_SUPERSECRETVALUE; Max-Age=600"
    fake = (
        FakeSwitch()
        .route(
            "GET",
            ROOT,
            httpx.Response(200, text=fixture("login_page.html"), headers={"set-cookie": cookie}),
        )
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
        .route("GET", POE, fixture("poe_config.html"))
    )
    client = build(tmp_path, fake)
    with caplog.at_level(logging.DEBUG, logger="tplink_easysmart_mcp"):
        await client.poe()
    text = caplog.text
    assert "TestPass123" not in text
    assert "SUPERSECRETVALUE" not in text

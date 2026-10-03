"""R2 / state machine — a classifier false positive becomes a session hazard.

Round 1 (F1, ``breaker/r1-parsers-forms``) proved that ``pages.classify`` /
``pages.is_login_page`` decide LOGIN_PAGE by a bare substring scan of the whole
body for ``var logonInfo`` or ``action="/logon.cgi"`` — so any reflected
user-controlled string (a VLAN name, the system description; the marker is 13
chars and fits the switch's 16-char limits) flips a valid DATA page to LOGIN_PAGE.

This file tests the *session-level* consequence, which the brief asks for. In
``SwitchClient.get_page`` a page that classifies as LOGIN_PAGE triggers
``mark_logged_out`` + one re-login + a re-GET. So a single ordinary read of a
page whose data happens to carry the marker:

1. spends a second ``POST /logon.cgi`` — and on this single-session switch every
   login **evicts the owner's live web-UI session** (spec §3.5); then
2. sees the same body again, still "a login page", and raises ``SESSION_LOST`` —
   an unrecoverable read until a human renames the offending VLAN/description.

Both tests assert the safe outcome (the valid page reads, with exactly one login)
and fail today. The marker is injected into an otherwise-valid, parseable PoE
page to stand in for the reflected VLAN-name / description vector of r1 F1.
No real device is contacted.
"""

from __future__ import annotations

import contextlib
import time

from tests.switch_fakes import FakeSwitch, fixture, make_settings
from tplink_easysmart_mcp.core.breaker import LoginBreaker
from tplink_easysmart_mcp.switch.auth import LoginCooldown
from tplink_easysmart_mcp.switch.client import SwitchClient
from tplink_easysmart_mcp.switch.errors import SessionLost

ROOT, LOGON, SYSINFO = "/", "/logon.cgi", "/SystemInfoRpm.htm"
POE = "/PoeConfigRpm.htm"
LOGOUT = "/Logout.htm"

# The 13-char login marker, as it would appear reflected inside a data page
# (e.g. a VLAN named "var logonInfo", or it in the device description).
REFLECTED_MARKER = "<!-- operator note: var logonInfo -->"


def _build(tmp_path, fake, **overrides) -> SwitchClient:
    settings = make_settings(tmp_path, **overrides)
    breaker = LoginBreaker(tmp_path, settings.host)
    cooldown = LoginCooldown(tmp_path, settings.host, settings.login_cooldown_s, now=time.time)
    return SwitchClient(settings, breaker, cooldown, transport=fake.transport())


def _hostile_poe_body() -> str:
    body = fixture("poe_config.html")
    assert "var portConfig" in body  # genuinely a DATA page (has its anchor)
    assert "var logonInfo" not in body  # clean before injection
    return body + "\n" + REFLECTED_MARKER


def _fake_with_hostile_poe() -> FakeSwitch:
    return (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
        .route("GET", LOGOUT, fixture("login_page.html"))
        .route("GET", POE, _hostile_poe_body())  # valid PoE data + reflected marker
    )


async def test_reflected_marker_data_page_still_reads(tmp_path) -> None:
    """A valid PoE page that merely contains the marker string must still parse."""
    client = _build(tmp_path, _fake_with_hostile_poe())
    snap = await client.poe()  # raises SESSION_LOST today
    assert snap.poe_port_num == 8


async def test_reflected_marker_does_not_cost_a_second_login(tmp_path) -> None:
    """A read must never spend a second logon POST, which evicts the owner's session."""
    fake = _fake_with_hostile_poe()
    client = _build(tmp_path, fake)
    with contextlib.suppress(SessionLost):
        await client.poe()
    # Exactly one login for one read. Today the spurious re-login makes it two,
    # kicking the owner's web UI off the single-session switch.
    assert fake.count("POST", LOGON) == 1

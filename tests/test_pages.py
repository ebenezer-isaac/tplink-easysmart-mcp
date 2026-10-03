"""Tests for body classification and login-page probing."""

from __future__ import annotations

from pathlib import Path

import pytest

from tplink_easysmart_mcp.switch.errors import ProtocolError
from tplink_easysmart_mcp.switch.models import AuthVariant, LoginMode, PageClass, SessionModel
from tplink_easysmart_mcp.switch.pages import classify, logon_err_type, probe_login_page

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


LOGIN_FIXTURES = [
    "login_page.html",
    "login_page_restored_account.html",
    "login_page_encrypted_variant.html",
    "logon_response_errtype0.html",
    "logon_response_errtype1_bad_credentials.html",
    "logon_response_errtype2_user_blocked.html",
    "logon_response_errtype4_sessions_full.html",
    "logon_response_errtype6_password_change.html",
    "poe_config_returned_login_page.html",
]

DATA_FIXTURES = {
    "system_info.html": "info_ds",
    "system_info_hostile_description.html": "info_ds",
    "port_setting.html": "all_info",
    "port_statistics.html": "all_info",
    "poe_config.html": "portConfig",
    "poe_config_truncated_arrays.html": "portConfig",
    "vlan_8021q.html": "qvlan_ds",
    "vlan_8021q_pvid.html": "pvid_ds",
}


@pytest.mark.parametrize("name", LOGIN_FIXTURES)
def test_login_pages_classify_as_login(name: str) -> None:
    assert classify(_fx(name), "portConfig") is PageClass.LOGIN_PAGE


@pytest.mark.parametrize(("name", "anchor"), DATA_FIXTURES.items())
def test_data_pages_classify_as_data(name: str, anchor: str) -> None:
    assert classify(_fx(name), anchor) is PageClass.DATA


def test_unexpected_body() -> None:
    assert classify("<html>nothing here</html>", "info_ds") is PageClass.UNEXPECTED
    assert classify("", "info_ds") is PageClass.UNEXPECTED


def test_wrong_anchor_is_unexpected_not_data() -> None:
    assert classify(_fx("system_info.html"), "portConfig") is PageClass.UNEXPECTED


# ---- errType ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("logon_response_errtype0.html", 0),
        ("logon_response_errtype1_bad_credentials.html", 1),
        ("logon_response_errtype2_user_blocked.html", 2),
        ("logon_response_errtype4_sessions_full.html", 4),
        ("logon_response_errtype6_password_change.html", 6),
        ("login_page.html", 0),
        ("login_page_restored_account.html", 6),
    ],
)
def test_logon_err_type(name: str, expected: int) -> None:
    assert logon_err_type(_fx(name)) == expected


def test_logon_info_missing_raises() -> None:
    with pytest.raises(ProtocolError):
        logon_err_type(_fx("system_info.html"))


def test_logon_info_non_int_raises() -> None:
    body = '<script>var logonInfo = new Array("x",0,0);</script>'
    with pytest.raises(ProtocolError):
        logon_err_type(body)


# ---- probe ------------------------------------------------------------------


def test_probe_normal_plain_form() -> None:
    probe = probe_login_page(_fx("login_page.html"))
    assert probe.auth_variant is AuthVariant.PLAIN_FORM
    assert probe.login_mode is LoginMode.NORMAL
    assert probe.err_type == 0
    assert probe.session_model is SessionModel.IP_BOUND


def test_probe_restored_account() -> None:
    probe = probe_login_page(_fx("login_page_restored_account.html"))
    assert probe.login_mode is LoginMode.RESTORED_ACCOUNT
    assert probe.err_type == 6


def test_probe_encrypted_variant() -> None:
    probe = probe_login_page(_fx("login_page_encrypted_variant.html"))
    assert probe.auth_variant is AuthVariant.ENCRYPTED


def test_submitform_is_not_an_encrypted_marker() -> None:
    # The plain page's form is name="submitForm"; that must not read as encrypted.
    assert 'name="submitForm"' in _fx("login_page.html")
    assert probe_login_page(_fx("login_page.html")).auth_variant is AuthVariant.PLAIN_FORM


def test_probe_cookie_session_model() -> None:
    headers = {"set-cookie": "H_P_SSID=tplink_abc; Max-Age=600"}
    assert probe_login_page(_fx("login_page.html"), headers).session_model is SessionModel.COOKIE


def test_probe_cookie_via_httpx_headers_get_list() -> None:
    import httpx

    headers = httpx.Headers([("set-cookie", "H_P_SSID=tplink_abc; Max-Age=600")])
    assert probe_login_page(_fx("login_page.html"), headers).session_model is SessionModel.COOKIE


def test_probe_no_cookie_header_is_ip_bound() -> None:
    import httpx

    headers = httpx.Headers([("content-type", "text/html")])
    assert probe_login_page(_fx("login_page.html"), headers).session_model is SessionModel.IP_BOUND


def test_probe_ip_bound_active_when_body_is_not_login() -> None:
    probe = probe_login_page(_fx("system_info.html"))
    assert probe.session_model is SessionModel.IP_BOUND_ACTIVE
    assert probe.auth_variant is AuthVariant.UNKNOWN
    assert probe.err_type is None

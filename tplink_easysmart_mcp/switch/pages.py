"""Pure body classification and login-page probing.

HTTP status says nothing about auth on this device: an unauthenticated request to
any page returns 200 plus the login page. Everything here keys on the body (and,
for the session model, the response headers), never on status or size.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .constants import ENCRYPTED_MARKERS, LOGON_FORM_ACTION, LOGON_INFO_VAR
from .errors import ProtocolError
from .jsvars import extract_vars
from .models import AuthVariant, LoginMode, LoginProbe, PageClass, SessionModel

ERR_TYPES: dict[int, str] = {
    0: "ok or silently ignored (verify with a data GET)",
    1: "the user name or the password is wrong",
    2: "the user is not allowed to login",
    3: "the number of users allowed to login is full",
    4: "the session table is full (16 users)",
    5: "the session is timeout",
    6: "restored-account (factory-reset) mode: changing a password, not logging in",
}

_SESSION_COOKIE = "H_P_SSID"


def is_login_page(html: str) -> bool:
    return f"var {LOGON_INFO_VAR}" in html or LOGON_FORM_ACTION in html


def classify(html: str, anchor: str) -> PageClass:
    """Classify a response body. Never raises; the parsers decide what to do."""
    if is_login_page(html):
        return PageClass.LOGIN_PAGE
    if re.search(rf"var\s+{re.escape(anchor)}\b", html):
        return PageClass.DATA
    return PageClass.UNEXPECTED


def logon_err_type(html: str) -> int:
    """Return ``logonInfo[0]``. Raises ProtocolError if it is absent or not an int."""
    info = extract_vars(html).get(LOGON_INFO_VAR)
    if not isinstance(info, list) or not info:
        raise ProtocolError("logonInfo is absent or not an array")
    first = info[0]
    if isinstance(first, bool) or not isinstance(first, int):
        raise ProtocolError("logonInfo[0] is not an integer")
    return first


def probe_login_page(html: str, headers: Mapping[str, Any] | None = None) -> LoginProbe:
    """Classify a ``GET /`` body into session model, auth variant and login mode."""
    login_page = is_login_page(html)
    err_type = logon_err_type(html) if (login_page and f"var {LOGON_INFO_VAR}" in html) else None
    return LoginProbe(
        session_model=_session_model(headers, login_page),
        auth_variant=_auth_variant(html, login_page),
        login_mode=_login_mode(html, err_type),
        err_type=err_type,
    )


def _auth_variant(html: str, login_page: bool) -> AuthVariant:
    if any(marker in html for marker in ENCRYPTED_MARKERS):
        return AuthVariant.ENCRYPTED
    if login_page:
        return AuthVariant.PLAIN_FORM
    return AuthVariant.UNKNOWN


def _login_mode(html: str, err_type: int | None) -> LoginMode:
    # The login page's static JS always contains `account_restored=1` inside its
    # errType==6 branch, so only a top-level assignment, a Confirm button, or
    # errType 6 itself is a real restored-account signal.
    if err_type == 6 or "var account_restored=1" in html or 'value="Confirm"' in html:
        return LoginMode.RESTORED_ACCOUNT
    return LoginMode.NORMAL


def _session_model(headers: Mapping[str, Any] | None, login_page: bool) -> SessionModel:
    if _has_session_cookie(headers):
        return SessionModel.COOKIE
    if login_page:
        return SessionModel.IP_BOUND
    # No cookie and the body is not the login page while the jar is empty: another
    # process on our IP already holds the session.
    return SessionModel.IP_BOUND_ACTIVE


def _has_session_cookie(headers: Mapping[str, Any] | None) -> bool:
    if not headers:
        return False
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        values = get_list("set-cookie")
    else:
        values = [v for k, v in headers.items() if str(k).lower() == "set-cookie"]
    return any(_SESSION_COOKIE in str(v) for v in values)

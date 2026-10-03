"""Pure body classification and login-page probing.

HTTP status says nothing about auth on this device: an unauthenticated request to
any page returns 200 plus the login page. Everything here keys on the body (and,
for the session model, the response headers), never on status or size.

Classification is *structural*, not substring-based. A login page is recognised
by a real ``var logonInfo = [...]`` declaration in the first ``<script>`` block
(read with the ``jsvars`` tokenizer) and/or a ``<form action="/logon.cgi">``
element (read with a tolerant HTML parser). Encrypted-variant markers count only
as script tokens or ``<script src>`` attributes, and restored-account mode only
from ``logonInfo[0] == 6`` or a real new-password field — never from a substring
inside a comment, an attribute, or a reflected string value (a VLAN name or a
device description), which an attacker on the cleartext LAN can set.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from .constants import ENCRYPTED_MARKERS, LOGON, LOGON_INFO_VAR
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

# HTML void elements never get an end tag, so they are never pushed on the nesting
# stack that tracks ``display:none`` ancestry.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def _attr_is_hidden(attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def _is_new_password_name(name: str) -> bool:
    """Name of a confirm/new-password field (the switch calls it ``cpassword``)."""
    low = name.lower()
    return low == "cpassword" or "confirm" in low or "newpass" in low or "new_password" in low


class _PageStructure(HTMLParser):
    """Collect the structural facts the classifier needs in one tolerant pass.

    ``html.parser`` treats ``<script>``/``<style>`` bodies as CDATA, so markup in
    a quoted script string, an HTML comment, or a reflected attribute never reaches
    ``handle_starttag`` and so can never be mistaken for a real element.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_actions: list[str] = []
        self.script_srcs: list[str] = []
        self.visible_password_names: list[str] = []
        self._script_chunks: list[str] = []
        self._in_script = False
        self._hidden_depth = 0
        self._hidden_stack: list[bool] = []

    # -- collection ----------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self._note(tag, a)
        if tag not in _VOID_TAGS:
            hidden = _attr_is_hidden(a)
            self._hidden_stack.append(hidden)
            if hidden:
                self._hidden_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._note(tag, dict(attrs))  # self-closing: nothing pushed on the stack

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False
        if tag not in _VOID_TAGS and self._hidden_stack and self._hidden_stack.pop():
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_chunks.append(data)

    def _note(self, tag: str, a: dict[str, str | None]) -> None:
        if tag == "script":
            self._in_script = True
            src = a.get("src")
            if src:
                self.script_srcs.append(src)
        elif tag == "form":
            self.form_actions.append((a.get("action") or "").strip())
        elif tag == "input" and (a.get("type") or "").strip().lower() == "password":
            name = (a.get("name") or "").strip()
            if name and self._hidden_depth == 0 and not _attr_is_hidden(a):
                self.visible_password_names.append(name)

    # -- derived facts -------------------------------------------------------
    @property
    def script_blob(self) -> str:
        return " ".join([*self._script_chunks, *self.script_srcs])

    @property
    def has_logon_form(self) -> bool:
        return any(action == LOGON for action in self.form_actions)

    @property
    def has_new_password_field(self) -> bool:
        return any(_is_new_password_name(name) for name in self.visible_password_names)


def _page_structure(html: str) -> _PageStructure:
    parser = _PageStructure()
    parser.feed(html)
    parser.close()
    return parser


def _declares_logon_info(html: str) -> bool:
    """True when the first script block declares ``var logonInfo = [...]``.

    Uses the ``jsvars`` tokenizer, so ``var logonInfo`` inside a string value (a
    VLAN name, a description) is not a declaration. A script block that will not
    tokenize (``ProtocolError``) declares nothing here; the form check still runs.
    """
    try:
        top = extract_vars(html)
    except ProtocolError:
        return False
    return isinstance(top.get(LOGON_INFO_VAR), list)


def is_login_page(html: str) -> bool:
    return _declares_logon_info(html) or _page_structure(html).has_logon_form


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
    structure = _page_structure(html)
    declares_logon = _declares_logon_info(html)
    login_page = declares_logon or structure.has_logon_form
    err_type = logon_err_type(html) if declares_logon else None
    return LoginProbe(
        session_model=_session_model(headers, login_page),
        auth_variant=_auth_variant(structure, login_page),
        login_mode=_login_mode(structure, err_type),
        err_type=err_type,
    )


def _auth_variant(structure: _PageStructure, login_page: bool) -> AuthVariant:
    # Markers only count as real script tokens or a <script src>; a marker inside
    # an HTML comment or ordinary text is CDATA-isolated and never seen here.
    blob = structure.script_blob
    if any(marker in blob for marker in ENCRYPTED_MARKERS):
        return AuthVariant.ENCRYPTED
    if login_page:
        return AuthVariant.PLAIN_FORM
    return AuthVariant.UNKNOWN


def _login_mode(structure: _PageStructure, err_type: int | None) -> LoginMode:
    # Restored-account mode is authoritative on logonInfo[0] == 6; the structural
    # fallback is a *visible* new-password field. A hidden static ``value="Confirm"``
    # input, revealed by JS only when errType == 6, is not a signal.
    if err_type == 6 or structure.has_new_password_field:
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

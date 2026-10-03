"""Authentication and session state for the Easy Smart switch.

The hard safety rules live here (master plan §1.3), now expressed through the one
canonical :class:`LoginBreaker` reservation (X1b core sync) instead of a separate
breaker + ``LoginCooldown`` file:

* **One explicit login per call path, never an automatic retry of a failed login.**
  Every login reserves exactly one attempt (``reserve_attempt``) before any POST;
  the reservation is held through ``GET /`` classification, the POST and the confirm.
* ``errType`` 1/2/6 and any unknown code resolve the reservation as a **failure**
  (``release(FAILURE)``) — at/over the budget the breaker trips and no further login
  runs until a human clears it.
* ``errType`` 3/4/5 resolve as **busy** (``release(BUSY, cooldown_s=...)``): a
  persisted cooldown in the one ledger, **not** a budget failure, so a full session
  table never walks the account into the hard lockout.
* ``errType`` 0 is **ambiguous** (a success and a silently-ignored POST are
  byte-identical, reference issue #49), so it is confirmed with one data GET; a
  confirmed login is ``release(SUCCESS)`` (which also clears any cooldown) and a
  silently-ignored one is ``release(FAILURE)``.
* A connection reset on the POST is resolved by the same confirm step, **never**
  by re-POSTing.
* Restored-account and encrypted-variant login pages fail closed with **no POST**,
  and because no credential ever reaches the device the reservation is cancelled
  with ``release(ABORT)`` — it does not spend the login budget.

Request logic is ported from ``vmakeev/hass_tplink_easy_smart`` (MIT, (c) 2022
Vladimir Makeev); none of its files are copied. The password and the ``logon.cgi``
request body are never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..core.breaker import LoginBreaker
from ..core.errors import TransportError
from ..core.state import Outcome, Reservation
from .config import SwitchSettings
from .constants import (
    ANCHOR_SYSTEM_INFO,
    LOGOUT,
    ROOT,
    SYSTEM_INFO,
)
from .errors import (
    AuthFailed,
    AuthVariantUnsupported,
    LockedOut,
    LoginNotAccepted,
    ProtocolError,
    RestoredAccountMode,
    SessionBusy,
    SessionsFull,
    SessionTimeout,
)
from .forms import build_login_form
from .models import AuthVariant, LoginMode, LoginProbe, PageClass, SessionModel, SystemInfo
from .pages import classify, logon_err_type, probe_login_page
from .parsers import parse_system_info

log = logging.getLogger(__name__)


class ConnectionResetSignal(Exception):
    """Internal marker: a mutating request's connection was reset mid-flight."""


@dataclass(frozen=True)
class HttpResult:
    text: str
    headers: httpx.Headers


class SwitchTransport(Protocol):
    """The subset of the client the authenticator drives."""

    def clear_cookies(self) -> None:
        """Empty the cookie jar (before every ``GET /``)."""

    async def raw_get(self, path: str) -> HttpResult:
        """GET ``path``; a reset/timeout raises ``TransportError``."""

    async def raw_post(self, path: str, fields: tuple[tuple[str, str], ...]) -> HttpResult:
        """POST a form; a reset/timeout raises ``ConnectionResetSignal``."""


@dataclass(frozen=True)
class LoginResult:
    system_info: SystemInfo
    session_model: SessionModel
    auth_variant: AuthVariant
    login_mode: LoginMode


class SwitchAuthenticator:
    """Drives the probe/login/logout state machine over an injected transport."""

    def __init__(
        self,
        settings: SwitchSettings,
        http: SwitchTransport,
        breaker: LoginBreaker,
    ) -> None:
        self._settings = settings
        self._http = http
        self._breaker = breaker
        self._authenticated = False
        self._session_model: SessionModel | None = None

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def session_model(self) -> SessionModel | None:
        return self._session_model

    def mark_logged_out(self) -> None:
        """Called when a data page comes back as the login page: the session is gone."""
        self._authenticated = False

    async def probe(self) -> LoginProbe:
        """One credential-free ``GET /`` + classification. No login, no gating."""
        self._http.clear_cookies()
        result = await self._http.raw_get(ROOT)
        probe = probe_login_page(result.text, result.headers)
        self._session_model = probe.session_model
        return probe

    async def login(self) -> LoginResult:
        """Make at most one ``POST /logon.cgi`` and confirm it. Never auto-retries.

        One attempt is reserved before any network I/O (this owns login-disabled, the
        persistent budget and the cooldown); the reservation is held through the
        credential-free ``GET /``, the POST and the confirm, and resolved exactly once.
        """
        res = self._breaker.reserve_attempt()
        posted = False
        try:
            self._http.clear_cookies()
            result = await self._http.raw_get(ROOT)
            probe = probe_login_page(result.text, result.headers)
            self._fail_closed(probe, res)
            self._session_model = probe.session_model

            plan = build_login_form(
                self._settings.username,
                self._settings.password.get_secret_value(),
                LoginMode.NORMAL,
            )
            posted = True
            try:
                response = await self._http.raw_post(plan.path, plan.fields)
            except ConnectionResetSignal:
                # The POST may or may not have landed; resolve by reading, never re-POST.
                return await self._confirm(probe, res)
            self._map_err_type(logon_err_type(response.text), res)
            return await self._confirm(probe, res)
        except BaseException:
            # Any path that did not resolve the reservation itself: once the POST was
            # attempted the attempt is spent (FAILURE, fail closed); before that no
            # credential reached the device (ABORT, do not spend the budget).
            if not res.resolved:
                res.release(Outcome.FAILURE if posted else Outcome.ABORT)
            raise

    async def logout(self) -> None:
        """``GET /Logout.htm`` and clear the jar. No I/O if not logged in; never raises."""
        if not self._authenticated:
            return
        try:
            await self._http.raw_get(LOGOUT)
        except (TransportError, ConnectionResetSignal) as exc:
            log.warning("logout request failed (%s); clearing local session", type(exc).__name__)
        finally:
            self._http.clear_cookies()
            self._authenticated = False

    # -- internals -------------------------------------------------------------

    def _fail_closed(self, probe: LoginProbe, res: Reservation) -> None:
        # A data body with an empty jar means another process on our IP is logged
        # in. The body is not a login page, so variant/mode are meaningless here:
        # check this first. No credential is sent, so cancel the reservation (ABORT):
        # nothing is wrong with us, the budget is not spent.
        if probe.session_model is SessionModel.IP_BOUND_ACTIVE:
            res.release(Outcome.ABORT)
            raise SessionBusy("another process on this IP already holds a session; not logging in")
        if probe.login_mode is not LoginMode.NORMAL:
            res.release(Outcome.ABORT)
            raise RestoredAccountMode(
                "switch is in restored-account (factory-reset) mode; no login attempted",
                err_type=probe.err_type,
            )
        if probe.auth_variant is not AuthVariant.PLAIN_FORM:
            res.release(Outcome.ABORT)
            raise AuthVariantUnsupported(
                f"login page is the {probe.auth_variant.value} variant, which is unsupported; "
                "no login attempted"
            )

    def _map_err_type(self, err: int, res: Reservation) -> None:
        if err == 0:
            return  # ambiguous; resolved by the confirm step
        if err == 1:
            res.release(Outcome.FAILURE, failure={"code": "AUTH_FAILED", "err_type": 1})
            raise AuthFailed("the username or password is wrong", err_type=1)
        if err == 2:
            res.release(Outcome.FAILURE, failure={"code": "LOCKED_OUT", "err_type": 2})
            raise LockedOut("the user is not allowed to login (locked out)", err_type=2)
        if err == 6:
            res.release(Outcome.FAILURE, failure={"code": "RESTORED_ACCOUNT_MODE", "err_type": 6})
            raise RestoredAccountMode(
                "the switch responded in restored-account mode; sending nothing more", err_type=6
            )
        if err in (3, 4):
            res.release(Outcome.BUSY, cooldown_s=self._settings.login_cooldown_s)
            raise SessionsFull("no free login slot on the switch", err_type=err)
        if err == 5:
            res.release(Outcome.BUSY, cooldown_s=self._settings.login_cooldown_s)
            raise SessionTimeout("the switch reported a session timeout", err_type=5)
        res.release(Outcome.FAILURE, failure={"code": "PROTOCOL_ERROR", "err_type": err})
        raise ProtocolError(f"unknown logon errType {err}")

    async def _confirm(self, probe: LoginProbe, res: Reservation) -> LoginResult:
        result = await self._http.raw_get(SYSTEM_INFO)
        if classify(result.text, ANCHOR_SYSTEM_INFO) is PageClass.DATA:
            self._authenticated = True
            res.release(Outcome.SUCCESS)
            return LoginResult(
                system_info=parse_system_info(result.text),
                session_model=self._session_model or probe.session_model,
                auth_variant=probe.auth_variant,
                login_mode=probe.login_mode,
            )
        res.release(Outcome.FAILURE, failure={"code": "LOGIN_NOT_ACCEPTED", "err_type": 0})
        raise LoginNotAccepted(
            "errType 0 but the confirm page was the login page (reference issue #49): "
            "the login was silently ignored"
        )

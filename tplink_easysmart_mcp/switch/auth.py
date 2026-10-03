"""Authentication and session state for the Easy Smart switch.

The hard safety rules live here (master plan §1.3):

* **One explicit login per call path, never an automatic retry of a failed login.**
* ``errType`` 1/2/6 and any unknown code trip the persistent breaker — they are
  never retried until a human clears it.
* ``errType`` 3/4/5 record a persisted cooldown instead of tripping the breaker.
* ``errType`` 0 is **ambiguous** (a success and a silently-ignored POST are
  byte-identical, reference issue #49), so it is confirmed with one data GET.
* A connection reset on the POST is resolved by the same confirm step, **never**
  by re-POSTing.
* Restored-account and encrypted-variant login pages fail closed with **no POST**.

Request logic is ported from ``vmakeev/hass_tplink_easy_smart`` (MIT, (c) 2022
Vladimir Makeev); none of its files are copied. The password and the ``logon.cgi``
request body are never logged.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..core.breaker import Clock, LoginBreaker
from ..core.errors import LockoutGuard, TransportError
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
    CooldownActive,
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

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(device: str) -> str:
    return (_UNSAFE.sub("_", device.strip()) or "device")[:64]


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


class LoginCooldown:
    """A persisted 'do not retry before T' timer for the soft login failures (3/4/5).

    Holds a single ``last_failure_at`` timestamp. Corrupt or unreadable state is
    treated as a fresh failure (fail closed), and a confirmed login clears it.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        device: str,
        seconds: int,
        *,
        now: Clock = time.time,
    ) -> None:
        self._path = Path(state_dir) / f"cooldown-{_slug(device)}.json"
        self._seconds = seconds
        self._now = now

    @property
    def path(self) -> Path:
        return self._path

    def check(self) -> None:
        remaining = self.remaining()
        if remaining > 0:
            raise CooldownActive(
                f"Login refused: a recent login failure is still in its "
                f"{self._seconds}s cooldown; retry in {remaining}s.",
                retry_after_s=remaining,
            )

    def remaining(self) -> int:
        last = self._last_failure_at()
        if last is None:
            return 0
        return max(0, math.ceil(self._seconds - (self._now() - last)))

    def record(self) -> None:
        self._atomic_write({"last_failure_at": self._now()})

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    def snapshot(self) -> dict[str, Any]:
        remaining = self.remaining()
        return {"active": remaining > 0, "retry_after_s": remaining}

    def _last_failure_at(self) -> float | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return self._now()  # unreadable ⇒ assume a failure just happened
        try:
            value = json.loads(raw)["last_failure_at"]
        except (ValueError, KeyError, TypeError):
            return self._now()  # corrupt ⇒ fail closed
        return float(value) if isinstance(value, int | float) else self._now()

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".cooldown-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


class SwitchAuthenticator:
    """Drives the probe/login/logout state machine over an injected transport."""

    def __init__(
        self,
        settings: SwitchSettings,
        http: SwitchTransport,
        breaker: LoginBreaker,
        cooldown: LoginCooldown,
    ) -> None:
        self._settings = settings
        self._http = http
        self._breaker = breaker
        self._cooldown = cooldown
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
        """Make at most one ``POST /logon.cgi`` and confirm it. Never auto-retries."""
        self._gate()
        self._http.clear_cookies()
        result = await self._http.raw_get(ROOT)
        probe = probe_login_page(result.text, result.headers)
        self._fail_closed(probe)
        self._session_model = probe.session_model

        plan = build_login_form(
            self._settings.username,
            self._settings.password.get_secret_value(),
            LoginMode.NORMAL,
        )
        try:
            response = await self._http.raw_post(plan.path, plan.fields)
        except ConnectionResetSignal:
            # The POST may or may not have landed; resolve by reading, never re-POST.
            return await self._confirm(probe)
        self._map_err_type(logon_err_type(response.text))
        return await self._confirm(probe)

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

    def _gate(self) -> None:
        if self._settings.login_disabled:
            raise LockoutGuard(
                f"Login refused: {self._settings.env_name('LOGIN_DISABLED')}=true freezes auth."
            )
        self._breaker.check()
        self._cooldown.check()

    def _fail_closed(self, probe: LoginProbe) -> None:
        # A data body with an empty jar means another process on our IP is logged
        # in. The body is not a login page, so variant/mode are meaningless here:
        # check this first, and do NOT trip the breaker (nothing is wrong with us).
        if probe.session_model is SessionModel.IP_BOUND_ACTIVE:
            raise SessionBusy("another process on this IP already holds a session; not logging in")
        if probe.login_mode is not LoginMode.NORMAL:
            self._breaker.record_failure({"reason": "restored_account", "err_type": probe.err_type})
            raise RestoredAccountMode(
                "switch is in restored-account (factory-reset) mode; no login attempted",
                err_type=probe.err_type,
            )
        if probe.auth_variant is not AuthVariant.PLAIN_FORM:
            self._breaker.record_failure(
                {"reason": "auth_variant", "auth_variant": probe.auth_variant.value}
            )
            raise AuthVariantUnsupported(
                f"login page is the {probe.auth_variant.value} variant, which is unsupported; "
                "no login attempted"
            )

    def _map_err_type(self, err: int) -> None:
        if err == 0:
            return
        if err == 1:
            self._breaker.record_failure({"code": "AUTH_FAILED", "err_type": 1})
            raise AuthFailed("the username or password is wrong", err_type=1)
        if err == 2:
            self._breaker.record_failure({"code": "LOCKED_OUT", "err_type": 2})
            raise LockedOut("the user is not allowed to login (locked out)", err_type=2)
        if err == 6:
            self._breaker.record_failure({"code": "RESTORED_ACCOUNT_MODE", "err_type": 6})
            raise RestoredAccountMode(
                "the switch responded in restored-account mode; sending nothing more", err_type=6
            )
        if err in (3, 4):
            self._cooldown.record()
            raise SessionsFull("no free login slot on the switch", err_type=err)
        if err == 5:
            self._cooldown.record()
            raise SessionTimeout("the switch reported a session timeout", err_type=5)
        self._breaker.record_failure({"code": "PROTOCOL_ERROR", "err_type": err})
        raise ProtocolError(f"unknown logon errType {err}")

    async def _confirm(self, probe: LoginProbe) -> LoginResult:
        result = await self._http.raw_get(SYSTEM_INFO)
        if classify(result.text, ANCHOR_SYSTEM_INFO) is PageClass.DATA:
            self._authenticated = True
            self._cooldown.clear()
            return LoginResult(
                system_info=parse_system_info(result.text),
                session_model=self._session_model or probe.session_model,
                auth_variant=probe.auth_variant,
                login_mode=probe.login_mode,
            )
        self._cooldown.record()
        raise LoginNotAccepted(
            "errType 0 but the confirm page was the login page (reference issue #49): "
            "the login was silently ignored"
        )

"""Switch-specific exceptions on top of the device-agnostic ``core.errors``.

Each class carries a stable ``kind`` used verbatim as the envelope error code, so
an LLM client can branch on a fixed vocabulary. The S1 parsing layer needs only
``ProtocolError``, ``SessionExpired`` and ``RestoredAccountMode``; S2 adds the
authentication/session codes.

Request and parsing logic is ported from ``vmakeev/hass_tplink_easy_smart``
(MIT, (c) 2022 Vladimir Makeev); none of its files are copied.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import DeviceError


class SwitchError(DeviceError):
    """Base class for every Easy Smart switch error."""

    kind = "SWITCH_ERROR"


class ProtocolError(SwitchError):
    """The page did not match the shape the parser requires.

    Raised for a missing/short array, a value outside its allowed set, an
    unparseable inline-variable block, or an ``UNEXPECTED`` body. Never raised
    merely because the session expired (that is ``SessionExpired``).
    """

    kind = "PROTOCOL_ERROR"


class SessionExpired(SwitchError):
    """A data page came back as the login page: the session is gone.

    The client re-logs in exactly once and retries; a second login page is a
    hard ``SESSION_LOST`` (mapped in S2).
    """

    kind = "SESSION_EXPIRED"


class RestoredAccountMode(SwitchError):
    """The switch is in factory-reset 'New Password / Confirm' mode.

    A POST to ``/logon.cgi`` in this mode would SET the admin password instead of
    authenticating, so the form builder refuses to produce a login body for any
    login mode other than ``NORMAL``.
    """

    kind = "RESTORED_ACCOUNT_MODE"

    def __init__(self, message: str, *, err_type: int | None = None) -> None:
        self.err_type = err_type
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"err_type": self.err_type}


class _ErrTypeError(SwitchError):
    """Base for the errType-mapped login errors; records the raw ``logonInfo``."""

    kind = "SWITCH_ERROR"

    def __init__(self, message: str, *, err_type: int | None = None) -> None:
        self.err_type = err_type
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"err_type": self.err_type}


class AuthFailed(_ErrTypeError):
    """errType 1: wrong username or password. Trips the breaker; never retried."""

    kind = "AUTH_FAILED"


class LockedOut(_ErrTypeError):
    """errType 2: the user is not allowed to log in. Trips the breaker."""

    kind = "LOCKED_OUT"


class SessionsFull(_ErrTypeError):
    """errType 3/4: no free login slot. Cooldown, not a breaker trip."""

    kind = "SESSIONS_FULL"


class SessionTimeout(_ErrTypeError):
    """errType 5: the session timed out. Cooldown, not a breaker trip."""

    kind = "SESSION_TIMEOUT"


class AuthVariantUnsupported(SwitchError):
    """The login page is the encrypted (or unknown) variant. Fail closed, no POST."""

    kind = "AUTH_VARIANT_UNSUPPORTED"


class LoginNotAccepted(SwitchError):
    """errType 0 but the confirm GET returned the login page (reference issue #49)."""

    kind = "LOGIN_NOT_ACCEPTED"


class SessionBusy(SwitchError):
    """``GET /`` returned a data page with an empty jar: another IP holds the session."""

    kind = "SESSION_BUSY"


class SessionLost(SwitchError):
    """A data page returned the login page twice: a single re-login did not recover it."""

    kind = "SESSION_LOST"


class CooldownActive(SwitchError):
    """A login was refused because a recent failure is still within the cooldown."""

    kind = "LOGIN_COOLDOWN"

    def __init__(self, message: str, *, retry_after_s: int) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"retry_after_s": self.retry_after_s}


class OutcomeUnknown(SwitchError):
    """A mutating request's connection was reset: the outcome must be re-read, not resent."""

    kind = "OUTCOME_UNKNOWN"

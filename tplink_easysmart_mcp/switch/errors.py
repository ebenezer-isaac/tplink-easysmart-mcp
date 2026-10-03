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


# --- S3 tool-layer errors (port resolution, write guards, power-cycle) --------


class UnknownPortName(SwitchError):
    """A port argument was a name that is not in ``EASYSMART_PORT_MAP``."""

    kind = "UNKNOWN_PORT"

    def __init__(self, message: str, *, known_names: list[str] | None = None) -> None:
        self.known_names = list(known_names or [])
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"known_names": self.known_names}


class AmbiguousPortName(SwitchError):
    """A port name matched more than one ``EASYSMART_PORT_MAP`` entry."""

    kind = "AMBIGUOUS_NAME"

    def __init__(
        self, message: str, *, name: str | None = None, ports: list[int] | None = None
    ) -> None:
        self.name = name
        self.ports = list(ports or [])
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"name": self.name, "ports": self.ports}


class InvalidPort(SwitchError):
    """A port argument was out of range (``1..max_port``) or not a port at all."""

    kind = "INVALID_PORT"

    def __init__(
        self, message: str, *, port: int | None = None, max_port: int | None = None
    ) -> None:
        self.port = port
        self.max_port = max_port
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"port": self.port, "max_port": self.max_port}


class NotPoePort(SwitchError):
    """A PoE write was asked for a port that does not carry PoE."""

    kind = "NOT_POE_PORT"

    def __init__(
        self,
        message: str,
        *,
        port: int | None = None,
        poe_ports: list[int] | None = None,
        poe_port_num: int | None = None,
    ) -> None:
        self.port = port
        self.poe_ports = list(poe_ports or [])
        self.poe_port_num = poe_port_num
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"port": self.port, "poe_ports": self.poe_ports, "poe_port_num": self.poe_port_num}


class ProtectedPort(SwitchError):
    """A write was refused because the port is in ``EASYSMART_PROTECTED_PORTS``."""

    kind = "PROTECTED_PORT"

    def __init__(
        self, message: str, *, port: int | None = None, protected_ports: list[int] | None = None
    ) -> None:
        self.port = port
        self.protected_ports = list(protected_ports or [])
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"port": self.port, "protected_ports": self.protected_ports}


class WriteVerifyFailed(SwitchError):
    """The re-read after a write did not match the intended change (or clobbered a field)."""

    kind = "WRITE_VERIFY_FAILED"

    def __init__(
        self,
        message: str,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        self._before = before
        self._after = after
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"before": self._before, "after": self._after}


class InvalidArgument(SwitchError):
    """A tool argument failed validation (for example ``off_seconds`` out of bounds)."""

    kind = "INVALID_ARGUMENT"

    def __init__(
        self,
        message: str,
        *,
        argument: str | None = None,
        allowed: dict[str, Any] | None = None,
    ) -> None:
        self.argument = argument
        self.allowed = dict(allowed or {})
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"argument": self.argument, "allowed": self.allowed}


class AlreadyOff(SwitchError):
    """A power-cycle was asked for a port whose PoE is already off; nothing to do."""

    kind = "ALREADY_OFF"

    def __init__(
        self,
        message: str,
        *,
        port: int | None = None,
        name: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.port = port
        self.name = name
        self._state = state
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"port": self.port, "name": self.name, "state": self._state}


class CycleInProgress(SwitchError):
    """Another power-cycle is already running (in-process lock or a fresh state marker)."""

    kind = "CYCLE_IN_PROGRESS"

    def __init__(
        self, message: str, *, started_at: float | None = None, age_s: float | None = None
    ) -> None:
        self.started_at = started_at
        self.age_s = age_s
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {"started_at": self.started_at, "age_s": self.age_s}


class CycleIncomplete(SwitchError):
    """A power-cycle could not be completed; the port may be left UNPOWERED."""

    kind = "CYCLE_INCOMPLETE"

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        self._context = dict(context or {})
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return dict(self._context)


class PowerNotRestored(SwitchError):
    """PoE was re-enabled but the PD did not draw power before the timeout."""

    kind = "POWER_NOT_RESTORED"

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        self._context = dict(context or {})
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return dict(self._context)


class NotSupported(SwitchError):
    """The feature's page is absent or not in the expected shape on this firmware."""

    kind = "NOT_SUPPORTED"

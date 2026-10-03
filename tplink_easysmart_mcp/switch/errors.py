"""Switch-specific exceptions used by the pure parsers and form builders.

These extend the device-agnostic ``core.errors`` hierarchy so they flow through
the shared envelope machinery. Phase S2 adds the authentication/session error
codes (AUTH_FAILED, SESSIONS_FULL, SESSION_BUSY, LOGIN_COOLDOWN, ...) on top of
this module; the three defined here are everything the S1 parsing layer needs.

Request and parsing logic is ported from ``vmakeev/hass_tplink_easy_smart``
(MIT, (c) 2022 Vladimir Makeev); none of its files are copied.
"""

from __future__ import annotations

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

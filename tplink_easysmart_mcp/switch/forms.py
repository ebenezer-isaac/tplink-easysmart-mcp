"""Pure form builders: current state + one change -> an exact request plan.

Every builder returns a ``RequestPlan`` with the method, path and ordered fields
the switch expects. Nothing here performs I/O; dry-run and logging render the
plan through ``plan_redacted`` so a password never reaches a log line.

Read-modify-write is mandatory: the PoE and port-setting handlers apply *every*
field, so the builders re-send the port's current priority/limit/speed and change
only the one thing asked for. Logic is ported from
``vmakeev/hass_tplink_easy_smart`` (MIT, (c) 2022 Vladimir Makeev); no files copied.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic import BaseModel, ConfigDict, Field

from .constants import (
    AUTO_LIMIT2,
    LOGON,
    POE_PORT_CONFIG_CGI,
    PORT_SETTING_CGI,
    POWERLIMIT_WRITE_CODE,
    PRESET_LIMIT2,
    PSTATE_DISABLE,
    PSTATE_ENABLE,
)
from .errors import RestoredAccountMode
from .models import LoginMode, PoeLimitKind, PoePort, PortState

REDACTED = "<redacted>"
_POE_PORT_MAX = 8  # PoE+ is on ports 1-8 of the TL-SG1016PE
_PORT_MAX = 64  # hard sanity bound; the live max_port_num is enforced by the tools


@dataclass(frozen=True)
class RequestPlan:
    method: str
    path: str
    fields: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, str]:
        """The fields as a dict (field order is preserved by insertion)."""
        return dict(self.fields)


class _PoeWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(ge=1, le=_POE_PORT_MAX)
    enabled: bool


class _PortWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(ge=1, le=_PORT_MAX)
    enabled: bool


def build_login_form(username: str, password: str, mode: LoginMode) -> RequestPlan:
    """Build the single ``POST /logon.cgi`` body. ``cpassword`` is always empty.

    Raises ``RestoredAccountMode`` for any mode other than ``NORMAL``: in
    restored-account mode a POST would SET the admin password, so no code path may
    produce a login body there.
    """
    if mode is not LoginMode.NORMAL:
        raise RestoredAccountMode(
            f"refusing to build a login body while the switch is in {mode.value} mode"
        )
    return RequestPlan(
        "POST",
        LOGON,
        (
            ("username", username),
            ("password", password),
            ("cpassword", ""),
            ("logon", "Login"),
        ),
    )


def build_poe_port_form(port: PoePort, enabled: bool) -> RequestPlan:
    """RMW the per-port PoE state: re-send the current priority and limit, change
    only ``name_pstate``. Selects exactly one port."""
    _PoeWriteInput(port=port.number, enabled=enabled)
    _, priority_raw, _ = port.raw
    code = POWERLIMIT_WRITE_CODE[port.limit_kind.value]
    return RequestPlan(
        "POST",
        POE_PORT_CONFIG_CGI,
        (
            (f"sel_{port.number}", "1"),
            ("name_pstate", str(PSTATE_ENABLE if enabled else PSTATE_DISABLE)),
            ("name_ppriority", str(priority_raw + 1)),
            ("name_ppowerlimit", str(code)),
            ("name_ppowerlimit2", _limit2(port, code)),
            ("applay", "Apply"),
        ),
    )


def build_port_setting_query(port: PortState, enabled: bool) -> RequestPlan:
    """RMW the per-port admin state via GET: re-send current spd_cfg and fc_cfg."""
    _PortWriteInput(port=port.number, enabled=enabled)
    spd_cfg, fc_cfg = port.raw
    return RequestPlan(
        "GET",
        PORT_SETTING_CGI,
        (
            ("portid", str(port.number)),
            ("state", "1" if enabled else "0"),
            ("speed", str(spd_cfg)),
            ("flowcontrol", str(fc_cfg)),
            ("apply", "Apply"),
        ),
    )


def plan_redacted(plan: RequestPlan) -> RequestPlan:
    """Return a copy of ``plan`` with any password field replaced by ``<redacted>``."""
    fields = tuple((name, REDACTED if name == "password" else value) for name, value in plan.fields)
    return replace(plan, fields=fields)


def _limit2(port: PoePort, code: int) -> str:
    if port.limit_kind is PoeLimitKind.AUTO:
        return AUTO_LIMIT2
    if port.limit_kind is PoeLimitKind.MANUAL:
        watts = port.limit_w if port.limit_w is not None else port.power_w
        return f"{watts:.1f}"
    return PRESET_LIMIT2[code]

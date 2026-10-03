"""Pydantic v2 models and enums for the Easy Smart switch pages (all frozen).

Enum ``from_code`` helpers raise ``ValueError`` for an out-of-range raw code; the
parsers translate that into a ``ProtocolError`` that names the page and value.
``PortSpeed`` is the one exception: unknown speed codes are kept as ``UNKNOWN``
(the raw code stays available on the owning model for read-modify-write).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PageClass(StrEnum):
    LOGIN_PAGE = "login_page"
    DATA = "data"
    UNEXPECTED = "unexpected"


class PortSpeed(StrEnum):
    LINK_DOWN = "link_down"
    AUTO = "auto"
    M10_HALF = "10M_half"
    M10_FULL = "10M_full"
    M100_HALF = "100M_half"
    M100_FULL = "100M_full"
    M1000_FULL = "1000M_full"
    UNKNOWN = "unknown"

    @classmethod
    def from_code(cls, code: int) -> PortSpeed:
        return _SPEED_BY_CODE.get(code, cls.UNKNOWN)


_SPEED_BY_CODE = {
    0: PortSpeed.LINK_DOWN,
    1: PortSpeed.AUTO,
    2: PortSpeed.M10_HALF,
    3: PortSpeed.M10_FULL,
    4: PortSpeed.M100_HALF,
    5: PortSpeed.M100_FULL,
    6: PortSpeed.M1000_FULL,
}


class PoePriority(StrEnum):
    HIGH = "high"
    MIDDLE = "middle"
    LOW = "low"

    @classmethod
    def from_code(cls, code: int) -> PoePriority:
        try:
            return _PRIORITY_BY_CODE[code]
        except KeyError:
            raise ValueError(f"unknown PoE priority code {code!r}") from None


_PRIORITY_BY_CODE = {0: PoePriority.HIGH, 1: PoePriority.MIDDLE, 2: PoePriority.LOW}


class PoeLimitKind(StrEnum):
    AUTO = "auto"
    CLASS1 = "class1"
    CLASS2 = "class2"
    CLASS3 = "class3"
    CLASS4 = "class4"
    MANUAL = "manual"


class PoeStatus(StrEnum):
    OFF = "off"
    TURNING_ON = "turning_on"
    ON = "on"
    OVERLOAD = "overload"
    SHORT = "short"
    NONSTANDARD_PD = "nonstandard_pd"
    VOLTAGE_HIGH = "voltage_high"
    VOLTAGE_LOW = "voltage_low"
    HARDWARE_FAULT = "hardware_fault"
    OVERTEMPERATURE = "overtemperature"

    @classmethod
    def from_code(cls, code: int) -> PoeStatus:
        try:
            return _STATUS_BY_CODE[code]
        except KeyError:
            raise ValueError(f"unknown PoE status code {code!r}") from None


_STATUS_BY_CODE = {
    0: PoeStatus.OFF,
    1: PoeStatus.TURNING_ON,
    2: PoeStatus.ON,
    3: PoeStatus.OVERLOAD,
    4: PoeStatus.SHORT,
    5: PoeStatus.NONSTANDARD_PD,
    6: PoeStatus.VOLTAGE_HIGH,
    7: PoeStatus.VOLTAGE_LOW,
    8: PoeStatus.HARDWARE_FAULT,
    9: PoeStatus.OVERTEMPERATURE,
}


class LoginMode(StrEnum):
    NORMAL = "normal"
    RESTORED_ACCOUNT = "restored_account"


class SessionModel(StrEnum):
    COOKIE = "cookie"
    IP_BOUND = "ip_bound"
    IP_BOUND_ACTIVE = "ip_bound_active"


class AuthVariant(StrEnum):
    PLAIN_FORM = "plain_form"
    ENCRYPTED = "encrypted"
    UNKNOWN = "unknown"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SystemInfo(_Frozen):
    name: str | None
    mac: str | None
    ip: str | None
    netmask: str | None
    gateway: str | None
    firmware: str | None
    hardware: str | None
    hw_revision: str | None


class PortState(_Frozen):
    number: int
    enabled: bool
    speed_config: PortSpeed
    speed_actual: PortSpeed
    link_up: bool
    fc_config: bool
    fc_actual: bool
    lag_id: int | None
    raw: tuple[int, int]  # (spd_cfg, fc_cfg) raw codes, for read-modify-write


class PortStats(_Frozen):
    number: int
    enabled: bool
    link: PortSpeed
    tx_good: int
    tx_bad: int
    rx_good: int
    rx_bad: int


class PoeBudget(_Frozen):
    limit_w: float
    consumption_w: float
    remain_w: float
    limit_min_w: float
    limit_max_w: float


class PoePort(_Frozen):
    number: int
    enabled: bool
    priority: PoePriority
    limit_kind: PoeLimitKind
    limit_w: float | None
    power_w: float
    current_ma: int
    voltage_v: float
    pd_class: str | None
    status: PoeStatus
    raw: tuple[int, int, int]  # (state, priority, powerlimit) raw codes, for RMW


class PoeSnapshot(_Frozen):
    poe_port_num: int
    budget: PoeBudget
    ports: tuple[PoePort, ...]


class Vlan(_Frozen):
    vid: int
    name: str
    tagged: tuple[int, ...]
    untagged: tuple[int, ...]


class VlanTable(_Frozen):
    enabled: bool
    port_count: int
    max_vids: int
    vlans: tuple[Vlan, ...]


class PortPvid(_Frozen):
    port: int
    pvid: int


class PvidTable(_Frozen):
    enabled: bool
    port_count: int
    pvids: tuple[PortPvid, ...]
    members_by_vid: dict[int, tuple[int, ...]]


class LoginProbe(_Frozen):
    session_model: SessionModel
    auth_variant: AuthVariant
    login_mode: LoginMode
    err_type: int | None

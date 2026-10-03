"""Read tools and the shared port-resolution / row-building helpers.

Every read logs in lazily through ``SwitchClient`` and never logs out unless
``EASYSMART_LOGOUT_AFTER_READS`` is set. Each op returns a plain dict that the
server wraps in the ``{success, data, error}`` envelope (and redacts); none of
them raise out of the tool — a device error becomes an envelope via ``run_tool``.

``resolve_port`` is the one place a port argument (an ``int`` or a
``EASYSMART_PORT_MAP`` name) becomes a port number, and it is reused by the write
and power-cycle tools so name handling and range checks are identical everywhere.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, ConfigDict

from .config import MAX_PORT, SwitchSettings
from .constants import ANCHOR_PVID, VLAN_PVID
from .errors import InvalidPort, NotSupported, ProtocolError, UnknownPortName
from .models import PoePort, PoeSnapshot, PoeStatus, PortState, PortStats, SessionModel
from .parsers import parse_pvids

if TYPE_CHECKING:
    from .backend import EasySmartSwitchBackend

# PoE statuses that mean a real fault (not merely "off" or "no PD").
FAULT_STATUSES: frozenset[PoeStatus] = frozenset(
    {
        PoeStatus.OVERLOAD,
        PoeStatus.SHORT,
        PoeStatus.VOLTAGE_HIGH,
        PoeStatus.VOLTAGE_LOW,
        PoeStatus.HARDWARE_FAULT,
        PoeStatus.OVERTEMPERATURE,
    }
)


class ResolvedPort(NamedTuple):
    port: int
    name: str | None


# --- input models ------------------------------------------------------------


class PortsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    only_linked: bool = False


class PortStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int | str | None = None


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name_or_port: int | str


# --- port resolution ---------------------------------------------------------


def resolve_port(arg: object, *, settings: SwitchSettings, max_port: int) -> ResolvedPort:
    """Turn an ``int`` or a ``PORT_MAP`` name into a validated ``(port, name)``.

    * A known name (case-insensitive) resolves to its mapped port.
    * A bare integer (or an all-ASCII-digit string) is range-checked.
    * An unknown name raises ``UnknownPortName`` listing the known names.
    * An out-of-range or non-port value raises ``InvalidPort``.
    """
    names = settings.port_map_dict
    if isinstance(arg, bool):  # bool is an int subclass; never a port
        raise InvalidPort("port must be an integer or a name, not a boolean", max_port=max_port)
    if isinstance(arg, int):
        return _checked(arg, settings, max_port)
    if isinstance(arg, str):
        text = arg.strip()
        key = text.lower()
        if key in names:
            return _checked(names[key], settings, max_port, name=key)
        if text.isascii() and text.isdigit():
            return _checked(int(text), settings, max_port)
        raise UnknownPortName(
            f"unknown port name {text[:64]!r}; known names: {sorted(names)}",
            known_names=sorted(names),
        )
    raise InvalidPort("port must be an integer or a name", max_port=max_port)


def _checked(
    port: int, settings: SwitchSettings, max_port: int, *, name: str | None = None
) -> ResolvedPort:
    if not 1 <= port <= max_port:
        raise InvalidPort(
            f"port {port} is out of range 1..{max_port}", port=port, max_port=max_port
        )
    if name is None:
        name = next((n for n, mapped in settings.port_map if mapped == port), None)
    return ResolvedPort(port, name)


def port_name(settings: SwitchSettings, port: int) -> str | None:
    return next((n for n, mapped in settings.port_map if mapped == port), None)


# --- row / view builders (shared with the write and cycle tools) -------------


def port_view(settings: SwitchSettings, p: PortState) -> dict[str, Any]:
    return {
        "port": p.number,
        "name": port_name(settings, p.number),
        "enabled": p.enabled,
        "link_up": p.link_up,
        "speed_actual": p.speed_actual.value,
        "speed_config": p.speed_config.value,
        "fc_config": p.fc_config,
        "fc_actual": p.fc_actual,
        "lag_id": p.lag_id,
        "protected": settings.is_protected(p.number),
    }


def stat_view(settings: SwitchSettings, s: PortStats) -> dict[str, Any]:
    return {
        "port": s.number,
        "name": port_name(settings, s.number),
        "enabled": s.enabled,
        "link": s.link.value,
        "tx_good": s.tx_good,
        "tx_bad": s.tx_bad,
        "rx_good": s.rx_good,
        "rx_bad": s.rx_bad,
    }


def poe_view(settings: SwitchSettings, p: PoePort) -> dict[str, Any]:
    return {
        "port": p.number,
        "name": port_name(settings, p.number),
        "enabled": p.enabled,
        "status": p.status.value,
        "power_w": p.power_w,
        "current_ma": p.current_ma,
        "voltage_v": p.voltage_v,
        "pd_class": p.pd_class,
        "pd_class_text": p.pd_class or "--",
        "priority": p.priority.value,
        "limit": {"kind": p.limit_kind.value, "w": p.limit_w},
        "protected": settings.is_protected(p.number),
    }


def budget_view(snap: PoeSnapshot) -> dict[str, Any]:
    b = snap.budget
    return {
        "limit_w": b.limit_w,
        "consumption_w": b.consumption_w,
        "remain_w": b.remain_w,
        "limit_min_w": b.limit_min_w,
        "limit_max_w": b.limit_max_w,
    }


# --- read ops ----------------------------------------------------------------


async def _reading(backend: EasySmartSwitchBackend, action: Callable[[], Awaitable[Any]]) -> Any:
    """Run a read under a session that logs out only if LOGOUT_AFTER_READS is set."""
    async with backend.client.session_scope(backend.settings.logout_after_reads):
        return await action()


async def get_system_info_op(backend: EasySmartSwitchBackend) -> dict[str, Any]:
    info = await _reading(backend, backend.client.system_info)
    sm: SessionModel | None = backend.client.authenticator.session_model
    return {
        "model": info.name,
        "hw_revision": info.hw_revision,
        "firmware": info.firmware,
        "hardware": info.hardware,
        "mac": info.mac,
        "ip": info.ip,
        "netmask": info.netmask,
        "gateway": info.gateway,
        "session_model": sm.value if sm else None,
    }


async def get_ports_op(
    backend: EasySmartSwitchBackend, *, only_linked: bool = False
) -> dict[str, Any]:
    args = PortsInput(only_linked=only_linked)
    ports = await _reading(backend, backend.client.ports)
    rows = [port_view(backend.settings, p) for p in ports if not args.only_linked or p.link_up]
    return {"count": len(rows), "only_linked": args.only_linked, "ports": rows}


async def get_port_stats_op(
    backend: EasySmartSwitchBackend, *, port: int | str | None = None
) -> dict[str, Any]:
    args = PortStatsInput(port=port)
    stats = await _reading(backend, backend.client.port_stats)
    if args.port is not None:
        resolved = resolve_port(args.port, settings=backend.settings, max_port=len(stats))
        return {"port": stat_view(backend.settings, stats[resolved.port - 1])}
    rows = [stat_view(backend.settings, s) for s in stats]
    error_ports = [s.number for s in stats if s.rx_bad + s.tx_bad > 0]
    return {"count": len(rows), "ports": rows, "error_ports": error_ports}


async def get_poe_op(backend: EasySmartSwitchBackend) -> dict[str, Any]:
    snap: PoeSnapshot = await _reading(backend, backend.client.poe)
    backend.observe_poe_port_num(snap.poe_port_num)
    rows = [poe_view(backend.settings, p) for p in snap.ports]
    fault_ports = [p.number for p in snap.ports if p.status in FAULT_STATUSES]
    unpowered = [
        p.number
        for p in snap.ports
        if p.enabled and p.power_w <= 0 and p.status not in FAULT_STATUSES
    ]
    return {
        "poe_port_num": snap.poe_port_num,
        "budget": budget_view(snap),
        "ports": rows,
        "fault_ports": fault_ports,
        "unpowered_enabled_ports": unpowered,
    }


async def get_vlans_op(backend: EasySmartSwitchBackend) -> dict[str, Any]:
    async with backend.client.session_scope(backend.settings.logout_after_reads):
        try:
            table = await backend.client.vlans()
        except ProtocolError as exc:
            raise NotSupported(
                "the 802.1Q VLAN page is absent or not in the expected shape on this firmware"
            ) from exc
        pvids = await _read_pvids(backend)
    vlans = [
        {
            "vid": v.vid,
            "name": v.name,
            "tagged": list(v.tagged),
            "untagged": list(v.untagged),
        }
        for v in table.vlans
    ]
    return {
        "enabled": table.enabled,
        "port_count": table.port_count,
        "max_vids": table.max_vids,
        "vlans": vlans,
        "pvids": pvids,
    }


async def _read_pvids(backend: EasySmartSwitchBackend) -> list[dict[str, int]] | None:
    try:
        html = await backend.client.get_page(VLAN_PVID, ANCHOR_PVID)
        table = parse_pvids(html)
    except ProtocolError:
        return None
    return [{"port": pp.port, "pvid": pp.pvid} for pp in table.pvids]


async def resolve_port_op(
    backend: EasySmartSwitchBackend, *, name_or_port: int | str
) -> dict[str, Any]:
    args = ResolveInput(name_or_port=name_or_port)
    ports = await _reading(backend, backend.client.ports)
    resolved = resolve_port(args.name_or_port, settings=backend.settings, max_port=len(ports))
    return {
        "port": resolved.port,
        "name": resolved.name,
        "is_poe": backend.settings.is_poe_port(resolved.port),
        "protected": backend.settings.is_protected(resolved.port),
        "max_port": len(ports),
    }


# ``MAX_PORT`` re-export: the write/cycle tools resolve a name before any network
# call, so they bound the resolve against the hard config maximum, then re-check
# against the live port count after the first read.
__all__ = [
    "FAULT_STATUSES",
    "MAX_PORT",
    "ResolvedPort",
    "budget_view",
    "get_poe_op",
    "get_port_stats_op",
    "get_ports_op",
    "get_system_info_op",
    "get_vlans_op",
    "poe_view",
    "port_name",
    "port_view",
    "resolve_port",
    "resolve_port_op",
]

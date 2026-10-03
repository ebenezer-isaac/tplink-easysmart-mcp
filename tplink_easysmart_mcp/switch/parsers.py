"""Pure page parsers: page HTML -> frozen pydantic models.

Each ``parse_X`` first classifies the body. A login page raises ``SessionExpired``
(never ``ProtocolError`` and never an empty result); an unexpected body or a
missing/short/ill-typed array raises ``ProtocolError`` naming the key and the
lengths. There are no partial rows and no I/O.

Logic is ported from ``vmakeev/hass_tplink_easy_smart`` (MIT, (c) 2022 Vladimir
Makeev); none of its files are copied.
"""

from __future__ import annotations

from typing import Any

from .constants import (
    ANCHOR_POE,
    ANCHOR_PORT_STATS,
    ANCHOR_PORTS,
    ANCHOR_PVID,
    ANCHOR_SYSTEM_INFO,
    ANCHOR_VLAN,
    PDCLASS_READ,
    POE_STATE_DISABLED,
    POE_STATE_ENABLED,
    POWERLIMIT_AUTO_RAW,
    PRESET_LIMIT_READ,
)
from .errors import ProtocolError, SessionExpired
from .jsvars import declared_count, extract_vars
from .models import (
    PageClass,
    PoeBudget,
    PoeLimitKind,
    PoePort,
    PoePriority,
    PoeSnapshot,
    PoeStatus,
    PortPvid,
    PortSpeed,
    PortState,
    PortStats,
    PvidTable,
    SystemInfo,
    Vlan,
    VlanTable,
)
from .pages import classify

# Watts implied by each class preset (used for display; the wire limit is identical).
_PRESET_LIMIT_W = {
    PoeLimitKind.CLASS1: 4.0,
    PoeLimitKind.CLASS2: 7.0,
    PoeLimitKind.CLASS3: 15.4,
    PoeLimitKind.CLASS4: 30.0,
}


def parse_system_info(html: str) -> SystemInfo:
    data = _require_data(html, ANCHOR_SYSTEM_INFO)
    info = data.get("info_ds")
    if not isinstance(info, dict):
        raise ProtocolError("info_ds is missing or not an object")

    def one(key: str) -> str | None:
        value = info.get(key)
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        return None

    hardware = one("hardwareStr")
    hw_revision = hardware.rsplit(" ", 1)[1] if hardware and " " in hardware else None
    return SystemInfo(
        name=one("descriStr"),
        mac=one("macStr"),
        ip=one("ipStr"),
        netmask=one("netmaskStr"),
        gateway=one("gatewayStr"),
        firmware=one("firmwareStr"),
        hardware=hardware,
        hw_revision=hw_revision,
    )


def parse_ports(html: str) -> tuple[PortState, ...]:
    data = _require_data(html, ANCHOR_PORTS)
    count = declared_count(data, "max_port_num")
    info = _req_obj(data, "all_info")
    state = _int_array(info.get("state"), "all_info.state", count)
    trunk = _int_array(info.get("trunk_info"), "all_info.trunk_info", count)
    spd_cfg = _int_array(info.get("spd_cfg"), "all_info.spd_cfg", count)
    spd_act = _int_array(info.get("spd_act"), "all_info.spd_act", count)
    fc_cfg = _int_array(info.get("fc_cfg"), "all_info.fc_cfg", count)
    fc_act = _int_array(info.get("fc_act"), "all_info.fc_act", count)
    return tuple(
        PortState(
            number=i + 1,
            enabled=state[i] == 1,
            speed_config=PortSpeed.from_code(spd_cfg[i]),
            speed_actual=PortSpeed.from_code(spd_act[i]),
            link_up=spd_act[i] != 0,
            fc_config=bool(fc_cfg[i]),
            fc_actual=bool(fc_act[i]),
            lag_id=trunk[i] or None,
            raw=(spd_cfg[i], fc_cfg[i]),
        )
        for i in range(count)
    )


def parse_port_stats(html: str) -> tuple[PortStats, ...]:
    data = _require_data(html, ANCHOR_PORT_STATS)
    count = declared_count(data, "max_port_num")
    info = _req_obj(data, "all_info")
    state = _int_array(info.get("state"), "all_info.state", count)
    link = _int_array(info.get("link_status"), "all_info.link_status", count)
    pkts = _int_array(info.get("pkts"), "all_info.pkts", count * 4)
    rows = []
    for i in range(count):
        base = i * 4
        rows.append(
            PortStats(
                number=i + 1,
                enabled=state[i] == 1,
                link=PortSpeed.from_code(link[i]),
                tx_good=pkts[base],
                tx_bad=pkts[base + 1],
                rx_good=pkts[base + 2],
                rx_bad=pkts[base + 3],
            )
        )
    return tuple(rows)


def parse_poe(html: str) -> PoeSnapshot:
    data = _require_data(html, ANCHOR_POE)
    count = declared_count(data, "poe_port_num")
    cfg = _req_obj(data, "portConfig")
    state = _int_array(cfg.get("state"), "portConfig.state", count)
    priority = _int_array(cfg.get("priority"), "portConfig.priority", count)
    powerlimit = _int_array(cfg.get("powerlimit"), "portConfig.powerlimit", count)
    power = _int_array(cfg.get("power"), "portConfig.power", count)
    current = _int_array(cfg.get("current"), "portConfig.current", count)
    voltage = _int_array(cfg.get("voltage"), "portConfig.voltage", count)
    pdclass = _int_array(cfg.get("pdclass"), "portConfig.pdclass", count)
    status = _int_array(cfg.get("powerstatus"), "portConfig.powerstatus", count)
    glb = _req_obj(data, "globalConfig")
    ports = tuple(
        _poe_port(i, state, priority, powerlimit, power, current, voltage, pdclass, status)
        for i in range(count)
    )
    budget = PoeBudget(
        limit_w=_req_int(glb, "system_power_limit") / 10,
        consumption_w=_req_int(glb, "system_power_consumption") / 10,
        remain_w=_req_int(glb, "system_power_remain") / 10,
        limit_min_w=_req_int(glb, "system_power_limit_min") / 10,
        limit_max_w=_req_int(glb, "system_power_limit_max") / 10,
    )
    return PoeSnapshot(poe_port_num=count, budget=budget, ports=ports)


def _poe_port(
    i: int,
    state: list[int],
    priority: list[int],
    powerlimit: list[int],
    power: list[int],
    current: list[int],
    voltage: list[int],
    pdclass: list[int],
    status: list[int],
) -> PoePort:
    st = state[i]
    if st not in (POE_STATE_DISABLED, POE_STATE_ENABLED):
        raise ProtocolError(f"portConfig.state[{i}]={st} is outside {{0, 1}}")
    for key, arr in (
        ("power", power),
        ("current", current),
        ("voltage", voltage),
        ("powerlimit", powerlimit),
    ):
        if arr[i] < 0:
            raise ProtocolError(f"portConfig.{key}[{i}]={arr[i]} is negative")
    kind, limit_w = _limit_from_raw(powerlimit[i])
    try:
        prio = PoePriority.from_code(priority[i])
        stat = PoeStatus.from_code(status[i])
    except ValueError as exc:
        raise ProtocolError(f"portConfig port {i + 1}: {exc}") from None
    return PoePort(
        number=i + 1,
        enabled=st == POE_STATE_ENABLED,
        priority=prio,
        limit_kind=kind,
        limit_w=limit_w,
        power_w=power[i] / 10,
        current_ma=current[i],
        voltage_v=voltage[i] / 10,
        pd_class=PDCLASS_READ.get(pdclass[i]),
        status=stat,
        raw=(st, priority[i], powerlimit[i]),
    )


def _limit_from_raw(raw: int) -> tuple[PoeLimitKind, float | None]:
    if raw == POWERLIMIT_AUTO_RAW:
        return PoeLimitKind.AUTO, None
    preset = PRESET_LIMIT_READ.get(raw)
    if preset is not None:
        kind = PoeLimitKind(preset)
        return kind, _PRESET_LIMIT_W[kind]
    return PoeLimitKind.MANUAL, raw / 10


def parse_vlans(html: str) -> VlanTable:
    data = _require_data(html, ANCHOR_VLAN)
    ds = _req_obj(data, "qvlan_ds")
    port_count = declared_count(ds, "portNum")
    count = _req_int(ds, "count")
    vids = _list_at_least(ds.get("vids"), "qvlan_ds.vids", count)
    names = _list_at_least(ds.get("names"), "qvlan_ds.names", count)
    tag = _int_array(ds.get("tagMbrs"), "qvlan_ds.tagMbrs", count)
    untag = _int_array(ds.get("untagMbrs"), "qvlan_ds.untagMbrs", count)
    vlans = tuple(
        Vlan(
            vid=_as_int(vids[i], "qvlan_ds.vids", i),
            name=str(names[i]),
            tagged=_ports_from_mask(tag[i], port_count),
            untagged=_ports_from_mask(untag[i], port_count),
        )
        for i in range(count)
    )
    return VlanTable(
        enabled=_req_int(ds, "state") == 1,
        port_count=port_count,
        max_vids=_req_int(ds, "maxVids"),
        vlans=vlans,
    )


def parse_pvids(html: str) -> PvidTable:
    data = _require_data(html, ANCHOR_PVID)
    ds = _req_obj(data, "pvid_ds")
    port_count = declared_count(ds, "portNum")
    count = _req_int(ds, "count")
    vids = _list_at_least(ds.get("vids"), "pvid_ds.vids", count)
    mbrs = _int_array(ds.get("mbrs"), "pvid_ds.mbrs", count)
    pvids = _int_array(ds.get("pvids"), "pvid_ds.pvids", port_count)
    port_pvids = tuple(PortPvid(port=p + 1, pvid=pvids[p]) for p in range(port_count))
    members = {
        _as_int(vids[i], "pvid_ds.vids", i): _ports_from_mask(mbrs[i], port_count)
        for i in range(count)
    }
    return PvidTable(
        enabled=_req_int(ds, "state") == 1,
        port_count=port_count,
        pvids=port_pvids,
        members_by_vid=members,
    )


# --- shared helpers ----------------------------------------------------------


def _require_data(html: str, anchor: str) -> dict[str, Any]:
    page = classify(html, anchor)
    if page is PageClass.LOGIN_PAGE:
        raise SessionExpired(f"the {anchor} page returned the login page")
    if page is not PageClass.DATA:
        raise ProtocolError(f"unexpected body for anchor {anchor!r}: {html[:120]!r}")
    return extract_vars(html)


def _req_obj(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ProtocolError(f"{key} is missing or not an object")
    return value


def _req_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key} is missing or not an integer")
    return value


def _int_array(value: object, key: str, min_len: int) -> list[int]:
    if not isinstance(value, list):
        raise ProtocolError(f"{key} is missing or not an array")
    if len(value) < min_len:
        raise ProtocolError(f"{key} too short: len {len(value)} < {min_len}")
    for idx, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ProtocolError(f"{key}[{idx}] is not an integer")
    return value


def _list_at_least(value: object, key: str, min_len: int) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{key} is missing or not an array")
    if len(value) < min_len:
        raise ProtocolError(f"{key} too short: len {len(value)} < {min_len}")
    return value


def _as_int(value: object, key: str, idx: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key}[{idx}] is not an integer")
    return value


def _ports_from_mask(mask: int, port_count: int) -> tuple[int, ...]:
    return tuple(p for p in range(1, port_count + 1) if mask & (1 << (p - 1)))

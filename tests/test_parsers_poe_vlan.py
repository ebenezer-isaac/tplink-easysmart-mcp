"""Tests for parse_poe, parse_vlans and parse_pvids against the MANIFEST."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tplink_easysmart_mcp.switch.errors import ProtocolError, SessionExpired
from tplink_easysmart_mcp.switch.parsers import parse_poe, parse_pvids, parse_vlans

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _script(body: str) -> str:
    return f"<script>\n{body}\n</script>"


_POE_FIELD = {
    "enabled": lambda p: p.enabled,
    "priority": lambda p: p.priority.value,
    "power_limit": lambda p: p.limit_kind.value,
    "power_limit_w": lambda p: p.limit_w,
    "power_w": lambda p: p.power_w,
    "current_ma": lambda p: p.current_ma,
    "voltage_v": lambda p: p.voltage_v,
    "pd_class": lambda p: p.pd_class,
    "status": lambda p: p.status.value,
}


def test_poe_matches_manifest() -> None:
    snap = parse_poe(_fx("poe_config.html"))
    expect = MANIFEST["fixtures"]["poe_config.html"]["expect"]
    assert snap.poe_port_num == expect["poe_port_num"]
    ports = {p.number: p for p in snap.ports}
    assert len(ports) == 8
    for num_str, fields in expect["ports"].items():
        port = ports[int(num_str)]
        for key, want in fields.items():
            if key == "case":
                continue
            assert _POE_FIELD[key](port) == want, f"port {num_str} {key}"


def test_poe_budget_matches_manifest() -> None:
    budget = parse_poe(_fx("poe_config.html")).budget
    want = MANIFEST["fixtures"]["poe_config.html"]["expect"]["budget"]
    assert budget.limit_w == want["limit_w"]
    assert budget.consumption_w == want["consumption_w"]
    assert budget.remain_w == want["remain_w"]
    assert budget.limit_min_w == want["limit_min_w"]
    assert budget.limit_max_w == want["limit_max_w"]


def test_poe_specific_cases() -> None:
    ports = {p.number: p for p in parse_poe(_fx("poe_config.html")).ports}
    assert ports[4].status.value == "off" and ports[4].power_w == 0.0  # unpowered, no PD
    assert ports[4].pd_class is None and ports[6].pd_class is None  # class "--"
    assert ports[7].status.value == "overload"  # over budget
    assert ports[5].limit_kind.value == "manual" and ports[5].limit_w == 25.5
    assert ports[8].enabled is False  # admin-disabled, state:0
    assert ports[8].raw == (0, 0, 330)  # raw tuple preserved for RMW


def test_poe_truncated_arrays_raise() -> None:
    with pytest.raises(ProtocolError, match=r"portConfig\.power too short: len 3 < 8"):
        parse_poe(_fx("poe_config_truncated_arrays.html"))


def test_poe_login_page_raises_session_expired() -> None:
    with pytest.raises(SessionExpired):
        parse_poe(_fx("poe_config_returned_login_page.html"))


def _poe_body(**arrays: str) -> str:
    base = {
        "state": "[1,1,1,1,1,1,1,0]",
        "priority": "[0,1,2,2,2,2,2,0]",
        "powerlimit": "[330,330,154,330,255,330,300,330]",
        "power": "[41,62,128,0,89,0,0,0]",
        "current": "[79,119,245,0,171,0,0,0]",
        "voltage": "[523,522,521,0,520,0,0,0]",
        "pdclass": "[330,70,154,0,300,0,300,0]",
        "powerstatus": "[2,2,2,0,2,5,3,0]",
    }
    base.update(arrays)
    cfg = ",".join(f"{k}:{v}" for k, v in base.items())
    return _script(
        "var poe_port_num = 8;\n"
        f"var portConfig = {{{cfg}}};\n"
        "var globalConfig = {system_power_limit:400,system_power_consumption:320,"
        "system_power_remain:80,system_power_limit_min:10,system_power_limit_max:1100};"
    )


def test_poe_negative_power_raises() -> None:
    with pytest.raises(ProtocolError, match="negative"):
        parse_poe(_poe_body(power="[-1,62,128,0,89,0,0,0]"))


def test_poe_float_power_raises() -> None:
    with pytest.raises(ProtocolError, match="not an integer"):
        parse_poe(_poe_body(power="[4.1,62,128,0,89,0,0,0]"))


def test_poe_state_outside_01_raises() -> None:
    with pytest.raises(ProtocolError, match=r"portConfig\.state\[0\]=2"):
        parse_poe(_poe_body(state="[2,1,1,1,1,1,1,0]"))


def test_poe_invalid_priority_code_raises() -> None:
    with pytest.raises(ProtocolError, match="priority"):
        parse_poe(_poe_body(priority="[9,1,2,2,2,2,2,0]"))


def test_poe_invalid_status_code_raises() -> None:
    with pytest.raises(ProtocolError, match="status"):
        parse_poe(_poe_body(powerstatus="[99,2,2,0,2,5,3,0]"))


def test_poe_missing_port_num_raises() -> None:
    body = _script("var portConfig = {state:[1]};var globalConfig = {};")
    with pytest.raises(ProtocolError, match="poe_port_num is missing"):
        parse_poe(body)


def test_poe_portconfig_not_object_raises() -> None:
    body = _script("var poe_port_num = 8;var portConfig = [1,2];")
    with pytest.raises(ProtocolError, match="portConfig is missing or not an object"):
        parse_poe(body)


def test_vlan_vids_not_a_list_raises() -> None:
    body = _script(
        "var qvlan_ds = {state:1,portNum:16,vids:5,count:1,maxVids:32,"
        "names:['Default'],tagMbrs:[0x0],untagMbrs:[0xffff]};"
    )
    with pytest.raises(ProtocolError, match=r"qvlan_ds\.vids"):
        parse_vlans(body)


def test_vlan_vid_non_int_raises() -> None:
    body = _script(
        "var qvlan_ds = {state:1,portNum:16,vids:['x'],count:1,maxVids:32,"
        "names:['Default'],tagMbrs:[0x0],untagMbrs:[0xffff]};"
    )
    with pytest.raises(ProtocolError, match="not an integer"):
        parse_vlans(body)


# ---- VLAN -------------------------------------------------------------------


def test_vlans_match_manifest() -> None:
    table = parse_vlans(_fx("vlan_8021q.html"))
    expect = MANIFEST["fixtures"]["vlan_8021q.html"]["expect"]
    assert table.enabled is expect["enabled"]
    assert table.port_count == expect["port_count"]
    assert table.max_vids == expect["max_vids"]
    got = {v.vid: v for v in table.vlans}
    for want in expect["vlans"]:
        v = got[want["vid"]]
        assert v.name == want["name"]
        assert list(v.untagged) == want["untagged"]
        assert list(v.tagged) == want["tagged"]


def test_vlan_count_respected_over_vids_length() -> None:
    body = _script(
        "var qvlan_ds = {state:1,portNum:16,vids:[1,10,20],count:2,maxVids:32,"
        "names:['Default','cams','mgmt'],tagMbrs:[0x0,0x8000,0x8000],"
        "untagMbrs:[0xff00,0xff,0x0]};"
    )
    table = parse_vlans(body)
    assert [v.vid for v in table.vlans] == [1, 10]


def test_vlan_disabled_state_zero() -> None:
    body = _script(
        "var qvlan_ds = {state:0,portNum:16,vids:[1],count:1,maxVids:32,"
        "names:['Default'],tagMbrs:[0x0],untagMbrs:[0xffff]};"
    )
    assert parse_vlans(body).enabled is False


def test_vlan_login_page_raises() -> None:
    with pytest.raises(SessionExpired):
        parse_vlans(_fx("poe_config_returned_login_page.html"))


# ---- PVID -------------------------------------------------------------------


def test_pvids_match_manifest() -> None:
    table = parse_pvids(_fx("vlan_8021q_pvid.html"))
    expect = MANIFEST["fixtures"]["vlan_8021q_pvid.html"]["expect"]
    pvids = {p.port: p.pvid for p in table.pvids}
    for port_str, want in expect["pvids"].items():
        assert pvids[int(port_str)] == want
    for vid_str, want in expect["members_by_vid"].items():
        assert list(table.members_by_vid[int(vid_str)]) == want

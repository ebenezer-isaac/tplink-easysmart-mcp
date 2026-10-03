"""Tests for parse_ports and parse_port_stats against the MANIFEST expectations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tplink_easysmart_mcp.switch.errors import ProtocolError, SessionExpired
from tplink_easysmart_mcp.switch.jsvars import extract_vars
from tplink_easysmart_mcp.switch.models import PortSpeed
from tplink_easysmart_mcp.switch.parsers import (
    parse_port_stats,
    parse_ports,
    parse_system_info,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _script(body: str) -> str:
    return f"<script>\n{body}\n</script>"


# ---- port settings -----------------------------------------------------------


def test_port_setting_matches_manifest() -> None:
    ports = {p.number: p for p in parse_ports(_fx("port_setting.html"))}
    assert len(ports) == 16
    expect = MANIFEST["fixtures"]["port_setting.html"]["expect"]["ports"]
    for num_str, fields in expect.items():
        port = ports[int(num_str)]
        for key, want in fields.items():
            if key == "enabled":
                assert port.enabled is want
            elif key == "link_up":
                assert port.link_up is want
            elif key == "speed_config":
                assert port.speed_config.value == want
            elif key == "speed_actual":
                assert port.speed_actual.value == want
            elif key == "fc_config":
                assert port.fc_config is want
            elif key == "fc_actual":
                assert port.fc_actual is want


def test_padding_is_eighteen_but_sixteen_ports_returned() -> None:
    all_info = extract_vars(_fx("port_setting.html"))["all_info"]
    assert len(all_info["state"]) == 18
    assert len(parse_ports(_fx("port_setting.html"))) == 16


def test_short_array_raises_naming_key_and_lengths() -> None:
    body = _script(
        "var max_port_num = 16;\n"
        "var all_info = {state:[1,1,1,1,1,1,1,1,1,1],"
        "trunk_info:[0,0,0,0,0,0,0,0,0,0],spd_cfg:[1,1,1,1,1,1,1,1,1,1],"
        "spd_act:[5,5,5,5,5,5,5,5,5,5],fc_cfg:[0,0,0,0,0,0,0,0,0,0],"
        "fc_act:[0,0,0,0,0,0,0,0,0,0]};"
    )
    with pytest.raises(ProtocolError, match=r"all_info\.state too short: len 10 < 16"):
        parse_ports(body)


def test_eight_port_switch_parses() -> None:
    body = _script(
        "var max_port_num = 8;\n"
        "var all_info = {state:[1,1,1,1,1,1,1,1,0,0],"
        "trunk_info:[0,0,0,0,0,0,0,0,0,0],spd_cfg:[1,1,1,1,1,1,1,1,0,0],"
        "spd_act:[6,6,6,6,6,6,6,6,0,0],fc_cfg:[0,0,0,0,0,0,0,0,0,0],"
        "fc_act:[0,0,0,0,0,0,0,0,0,0]};"
    )
    ports = parse_ports(body)
    assert len(ports) == 8
    assert ports[0].speed_actual is PortSpeed.M1000_FULL


def test_unknown_speed_codes_kept_as_unknown() -> None:
    body = _script(
        "var max_port_num = 2;\n"
        "var all_info = {state:[1,1,0,0],trunk_info:[0,0,0,0],"
        "spd_cfg:[7,8,0,0],spd_act:[7,8,0,0],fc_cfg:[0,0,0,0],fc_act:[0,0,0,0]};"
    )
    ports = parse_ports(body)
    assert ports[0].speed_config is PortSpeed.UNKNOWN
    assert ports[1].speed_actual is PortSpeed.UNKNOWN
    assert ports[0].raw == (7, 0)  # raw code preserved for read-modify-write


def test_login_page_raises_session_expired() -> None:
    with pytest.raises(SessionExpired):
        parse_ports(_fx("poe_config_returned_login_page.html"))


# ---- port statistics ---------------------------------------------------------


def test_port_statistics_matches_manifest() -> None:
    rows = {r.number: r for r in parse_port_stats(_fx("port_statistics.html"))}
    expect = MANIFEST["fixtures"]["port_statistics.html"]["expect"]["ports"]
    for num_str, fields in expect.items():
        row = rows[int(num_str)]
        for key, want in fields.items():
            if key == "enabled":
                assert row.enabled is want
            elif key == "link":
                assert row.link.value == want
            else:
                assert getattr(row, key) == want


def test_port_statistics_pkts_length() -> None:
    pkts = extract_vars(_fx("port_statistics.html"))["all_info"]["pkts"]
    assert len(pkts) == MANIFEST["fixtures"]["port_statistics.html"]["expect"]["pkts_len"]


def test_port_statistics_login_page_raises() -> None:
    with pytest.raises(SessionExpired):
        parse_port_stats(_fx("poe_config_returned_login_page.html"))


# ---- system info -------------------------------------------------------------


def test_system_info_matches_manifest() -> None:
    info = parse_system_info(_fx("system_info.html"))
    expect = MANIFEST["fixtures"]["system_info.html"]["expect"]
    assert info.name == expect["name"]
    assert info.mac == expect["mac"]
    assert info.ip == expect["ip"]
    assert info.netmask == expect["netmask"]
    assert info.gateway == expect["gateway"]
    assert info.firmware == expect["firmware"]
    assert info.hardware == expect["hardware"]
    assert info.hw_revision == expect["hw_revision"]


def test_system_info_hostile_description() -> None:
    info = parse_system_info(_fx("system_info_hostile_description.html"))
    assert (
        info.name == MANIFEST["fixtures"]["system_info_hostile_description.html"]["expect"]["name"]
    )


def test_system_info_non_single_array_yields_none() -> None:
    body = _script('var info_ds = {descriStr:["a","b"],hardwareStr:["TL-SG1016PE"]};')
    info = parse_system_info(body)
    assert info.name is None  # length-2 array is not a one-element field
    assert info.hardware == "TL-SG1016PE"
    assert info.hw_revision is None  # no space to split on


def test_system_info_wrong_shape_raises() -> None:
    with pytest.raises(ProtocolError, match="info_ds is missing or not an object"):
        parse_system_info(_script("var info_ds = [1,2,3];"))


def test_system_info_login_page_raises() -> None:
    with pytest.raises(SessionExpired):
        parse_system_info(_fx("poe_config_returned_login_page.html"))

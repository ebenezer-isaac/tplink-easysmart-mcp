"""Read tools + port resolution against the stateful fake switch."""

from __future__ import annotations

import json

import httpx

from .switch_fakes import StatefulSwitch, build_mcp, call, read_backend

POE = "/PoeConfigRpm.htm"
VLAN = "/Vlan8021QRpm.htm"


def mcp_for(tmp_path, switch, **overrides):
    return build_mcp(read_backend(tmp_path, switch, **overrides))


# ---- each read fixture -> model ---------------------------------------------


async def test_get_system_info(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch())
    env = await call(mcp, "switch_get_system_info")
    assert env["success"] is True
    assert env["data"]["model"] == "TL-SG1016PE"
    assert env["data"]["hw_revision"] == "3.0"
    assert env["data"]["session_model"] == "ip_bound"


async def test_get_ports_all_and_only_linked(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch(), port_map="cam1=1")
    all_ports = await call(mcp, "switch_get_ports")
    assert all_ports["data"]["count"] == 16
    assert all_ports["data"]["ports"][0]["name"] == "cam1"
    linked = await call(mcp, "switch_get_ports", {"only_linked": True})
    assert all(row["link_up"] for row in linked["data"]["ports"])
    assert linked["data"]["count"] < 16


async def test_get_port_stats_all_lists_error_ports(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch())
    env = await call(mcp, "switch_get_port_stats")
    assert env["data"]["ports"][0]["tx_good"] == 7013
    assert 6 in env["data"]["error_ports"]  # port 6 has tx_bad/rx_bad > 0


async def test_get_port_stats_single_port(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch())
    env = await call(mcp, "switch_get_port_stats", {"port": 1})
    assert env["data"]["port"]["port"] == 1
    assert env["data"]["port"]["tx_good"] == 7013


async def test_get_poe_rows_budget_and_derived(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch(), port_map="cam1=1")
    env = await call(mcp, "switch_get_poe")
    data = env["data"]
    assert data["poe_port_num"] == 8
    assert data["budget"]["limit_w"] == 40.0
    # -- class rendering for ports with no PD class
    assert data["ports"][3]["pd_class"] is None
    assert data["ports"][3]["pd_class_text"] == "--"
    assert data["ports"][5]["pd_class_text"] == "--"
    # fault_ports: port 7 is overload; port 6 (nonstandard_pd) is NOT a fault
    assert 7 in data["fault_ports"]
    assert 6 not in data["fault_ports"]
    # unpowered-but-enabled: ports 4 and 6
    assert data["unpowered_enabled_ports"] == [4, 6]
    assert data["ports"][0]["name"] == "cam1"


async def test_get_vlans(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch())
    env = await call(mcp, "switch_get_vlans")
    data = env["data"]
    assert data["enabled"] is True
    assert {v["vid"] for v in data["vlans"]} == {1, 10, 20}
    assert any(pp["port"] == 1 and pp["pvid"] == 10 for pp in data["pvids"])


# ---- name resolution --------------------------------------------------------


async def test_resolve_port_by_name_case_insensitive(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch(), port_map="Cam1=1;uplink=16")
    env = await call(mcp, "switch_resolve_port", {"name_or_port": "CAM1"})
    assert env["success"] is True
    assert env["data"]["port"] == 1
    assert env["data"]["is_poe"] is True
    assert env["data"]["protected"] is False


async def test_resolve_port_unknown_name_lists_known(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch(), port_map="cam1=1")
    env = await call(mcp, "switch_resolve_port", {"name_or_port": "nope"})
    assert env["success"] is False
    assert env["error"]["code"] == "UNKNOWN_PORT"
    assert env["error"]["details"]["known_names"] == ["cam1"]


async def test_resolve_port_out_of_range(tmp_path) -> None:
    mcp = mcp_for(tmp_path, StatefulSwitch())
    env = await call(mcp, "switch_resolve_port", {"name_or_port": 99})
    assert env["error"]["code"] == "INVALID_PORT"


# ---- session eviction mid-read ----------------------------------------------


async def test_login_page_mid_read_triggers_one_relogin(tmp_path) -> None:
    switch = StatefulSwitch()
    backend = read_backend(tmp_path, switch)
    mcp = build_mcp(backend)
    # Prime a logged-in session, then evict so the next data GET returns the login page.
    await call(mcp, "switch_get_poe")
    logins_before = switch.logins
    switch.evict()
    env = await call(mcp, "switch_get_poe")
    assert env["success"] is True
    assert switch.logins == logins_before + 1  # exactly one re-login


# ---- adversarial / malformed bodies -----------------------------------------


async def test_vlan_page_missing_returns_not_supported(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.vlan_unexpected = True
    mcp = build_mcp(read_backend(tmp_path, switch))
    env = await call(mcp, "switch_get_vlans")
    assert env["success"] is False
    assert env["error"]["code"] == "NOT_SUPPORTED"


async def test_malformed_poe_page_is_protocol_error_envelope(tmp_path) -> None:
    # A PoE page whose arrays are too short is a PROTOCOL_ERROR envelope, not a crash.
    class BadSwitch(StatefulSwitch):
        def _render_poe(self) -> str:
            return (
                "<!DOCTYPE html>\n<script>\nvar poe_port_num = 8;\n"
                "var portConfig = {state:[1,1,1]};\nvar globalConfig = {};\n</script>"
            )

    mcp = build_mcp(read_backend(tmp_path, BadSwitch()))
    env = await call(mcp, "switch_get_poe")
    assert env["success"] is False
    assert env["error"]["code"] == "PROTOCOL_ERROR"


async def test_read_tool_does_not_log_out_by_default(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = build_mcp(read_backend(tmp_path, switch))
    await call(mcp, "switch_get_poe")
    assert switch.count("GET", "/Logout.htm") == 0


async def test_read_tool_logs_out_when_configured(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = build_mcp(read_backend(tmp_path, switch, logout_after_reads="true"))
    await call(mcp, "switch_get_poe")
    assert switch.count("GET", "/Logout.htm") == 1


async def test_envelope_has_no_password_or_cookie(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = build_mcp(read_backend(tmp_path, switch, password="TopSecret99"))
    env = await call(mcp, "switch_get_system_info")
    assert "TopSecret99" not in json.dumps(env)


async def test_unreachable_switch_is_envelope_not_exception(tmp_path) -> None:
    from tplink_easysmart_mcp.switch.backend import EasySmartSwitchBackend

    from .switch_fakes import make_settings

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    backend = EasySmartSwitchBackend(make_settings(tmp_path), transport=httpx.MockTransport(boom))
    env = await call(build_mcp(backend), "switch_get_system_info")
    assert env["success"] is False
    assert env["error"]["code"] == "TRANSPORT_ERROR"

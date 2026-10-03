"""Write tools (switch_set_poe / switch_set_port): gating, RMW, verify, dry-run."""

from __future__ import annotations

import json

from tplink_easysmart_mcp.switch.constants import AUTO_LIMIT2

from .switch_fakes import StatefulSwitch, build_mcp, call, read_backend, write_backend

POE_CGI = "/poe_port_config.cgi"
PORT_CGI = "/port_setting.cgi"
LOGOUT = "/Logout.htm"


def wmcp(tmp_path, switch, **overrides):
    backend, _ = write_backend(tmp_path, switch, **overrides)
    return build_mcp(backend)


# ---- the two write gates (no network at all when refused) --------------------


async def test_writes_disabled_refuses_with_zero_requests(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = build_mcp(read_backend(tmp_path, switch))  # ALLOW_WRITES is false
    env = await call(mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": True})
    assert env["success"] is False
    assert env["error"]["code"] == "WRITE_REFUSED"
    assert switch.calls == []


async def test_confirm_write_false_refuses_with_zero_requests(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": False})
    assert env["error"]["code"] == "WRITE_REFUSED"
    assert switch.calls == []


# ---- config-only refusals (no network) --------------------------------------


async def test_non_poe_port_refused_before_any_request(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_poe", {"port": 9, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "NOT_POE_PORT"
    assert switch.calls == []


async def test_protected_port_refused_before_any_request(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch, protected_ports="1,16")
    env = await call(mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "NOT_POE_PORT" or env["error"]["code"] == "PROTECTED_PORT"
    # port 1 IS a PoE port here, so it must be PROTECTED_PORT
    assert env["error"]["code"] == "PROTECTED_PORT"
    assert switch.calls == []


async def test_set_port_protected_refused(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)  # protected_ports defaults to 16
    env = await call(mcp, "switch_set_port", {"port": 16, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "PROTECTED_PORT"
    assert switch.calls == []


async def test_injected_name_is_unknown_port_with_no_request(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    for bad in ["1;sel_2=1", "../", "x" * 10_000, "café"]:
        env = await call(
            mcp, "switch_set_poe", {"port": bad, "enabled": False, "confirm_write": True}
        )
        assert env["error"]["code"] == "UNKNOWN_PORT"
    assert switch.calls == []


# ---- read-modify-write body + verify ----------------------------------------


async def test_set_poe_rmw_body_matches_manifest_and_succeeds(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch, port_map="cam1=1")
    env = await call(
        mcp, "switch_set_poe", {"port": "cam1", "enabled": False, "confirm_write": True}
    )
    assert env["success"] is True
    assert env["data"]["changed"] is True
    assert env["data"]["before"]["enabled"] is True
    assert env["data"]["after"]["enabled"] is False
    # Exactly the manifest port1_off body (priority/limit re-sent unchanged).
    assert switch.poe_write_bodies() == [
        {
            "sel_1": "1",
            "name_pstate": "1",
            "name_ppriority": "1",
            "name_ppowerlimit": "1",
            "name_ppowerlimit2": AUTO_LIMIT2,
            "applay": "Apply",
        }
    ]
    assert switch.count("GET", LOGOUT) == 1  # logout after success


async def test_set_poe_already_in_state_sends_no_write(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_poe", {"port": 8, "enabled": False, "confirm_write": True})
    assert env["success"] is True
    assert env["data"]["changed"] is False
    assert switch.count("POST", POE_CGI) == 0


async def test_set_poe_clobbered_priority_is_verify_failed(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.clobber_poe_priority = True
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": True})
    assert env["success"] is False
    assert env["error"]["code"] == "WRITE_VERIFY_FAILED"
    assert (
        env["error"]["details"]["before"]["priority"]
        != env["error"]["details"]["after"]["priority"]
    )
    assert switch.count("GET", LOGOUT) == 1  # logout even after failure


async def test_set_poe_connection_reset_is_outcome_unknown(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.reset_on_poe_write = 1
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "OUTCOME_UNKNOWN"
    assert switch.count("GET", LOGOUT) == 1


async def test_set_poe_dry_run_sends_no_write_and_returns_form(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch, dry_run="true", port_map="cam1=1")
    env = await call(
        mcp, "switch_set_poe", {"port": "cam1", "enabled": False, "confirm_write": True}
    )
    assert env["success"] is True
    assert env["data"]["dry_run"] is True
    assert env["data"]["planned"]["name_pstate"] == "1"
    assert env["data"]["planned"]["name_ppriority"] == "1"
    assert switch.count("POST", POE_CGI) == 0


async def test_set_port_rmw_and_verify(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_port", {"port": 5, "enabled": False, "confirm_write": True})
    assert env["success"] is True
    assert env["data"]["after"]["enabled"] is False
    # spd_cfg/fc_cfg re-sent unchanged: GET /port_setting.cgi?portid=5&state=0&speed=1&flowcontrol=0
    req = next(r for r in switch.calls if r.url.path == PORT_CGI)
    assert req.url.params["portid"] == "5"
    assert req.url.params["state"] == "0"
    assert req.url.params["speed"] == "1"
    assert switch.count("GET", LOGOUT) == 1


async def test_set_port_clobbered_speed_is_verify_failed(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.clobber_port_speed = True
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_port", {"port": 5, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "WRITE_VERIFY_FAILED"


async def test_set_poe_above_live_poe_port_num_is_not_poe(tmp_path) -> None:
    switch = StatefulSwitch()
    switch.poe_port_num = 6  # the switch reports fewer PoE ports than configured
    mcp = wmcp(tmp_path, switch, poe_ports="1-8")
    env = await call(mcp, "switch_set_poe", {"port": 7, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "NOT_POE_PORT"
    assert env["error"]["details"]["poe_port_num"] == 6
    assert switch.count("POST", POE_CGI) == 0


async def test_set_port_already_in_state_sends_no_write(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_port", {"port": 12, "enabled": False, "confirm_write": True})
    assert env["success"] is True
    assert env["data"]["changed"] is False
    assert switch.count("GET", PORT_CGI) == 0


async def test_set_port_dry_run_sends_no_write(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch, dry_run="true")
    env = await call(mcp, "switch_set_port", {"port": 5, "enabled": False, "confirm_write": True})
    assert env["data"]["dry_run"] is True
    assert env["data"]["planned"]["portid"] == "5"
    assert env["data"]["planned"]["state"] == "0"
    assert switch.count("GET", PORT_CGI) == 0


async def test_set_port_above_max_port_is_invalid_port(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch)
    env = await call(mcp, "switch_set_port", {"port": 20, "enabled": False, "confirm_write": True})
    assert env["error"]["code"] == "INVALID_PORT"
    assert switch.count("GET", PORT_CGI) == 0


async def test_write_envelope_has_no_password(tmp_path) -> None:
    switch = StatefulSwitch()
    mcp = wmcp(tmp_path, switch, password="TopSecret99")
    env = await call(mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": True})
    assert "TopSecret99" not in json.dumps(env)

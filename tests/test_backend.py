"""Backend + server-tool integration against the in-process fake switch."""

from __future__ import annotations

import json
from typing import Any

import httpx

from tplink_easysmart_mcp.core.config import load_global_settings
from tplink_easysmart_mcp.server import MCP_ENV_PREFIX, build_server
from tplink_easysmart_mcp.switch.backend import EasySmartSwitchBackend

from .switch_fakes import FakeSwitch, fixture, make_settings

ROOT, LOGON, SYSINFO = "/", "/logon.cgi", "/SystemInfoRpm.htm"


def backend_for(tmp_path, fake, **overrides) -> EasySmartSwitchBackend:
    settings = make_settings(tmp_path, **overrides)
    return EasySmartSwitchBackend(settings, transport=fake.transport())


async def _call(mcp, name: str) -> dict[str, Any]:
    result = await mcp.call_tool(name, {})
    structured = result[1] if isinstance(result, tuple) else result
    if isinstance(structured, dict) and "result" in structured and "success" not in structured:
        structured = structured["result"]
    if isinstance(structured, dict) and "success" in structured:
        return structured
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# ---- healthcheck (switch_status) --------------------------------------------


async def test_status_probe_makes_one_get_and_no_post(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("login_page.html"))
    backend = backend_for(tmp_path, fake)
    data = await backend.healthcheck()
    assert fake.count("GET", ROOT) == 1
    assert fake.count("POST", LOGON) == 0
    assert data["reachable"] is True
    assert data["session_model"] == "ip_bound"
    assert data["breaker"]["state"] == "closed"


async def test_status_reports_unreachable_without_raising(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, httpx.ConnectError("down"))
    backend = backend_for(tmp_path, fake)
    data = await backend.healthcheck()
    assert data["reachable"] is False
    assert "probe_error" in data


async def test_status_reports_poe_port_mismatch(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("login_page.html"))
    backend = backend_for(tmp_path, fake)  # default POE_PORTS = 1-8
    assert backend.observe_poe_port_num(4) is not None
    data = await backend.healthcheck()
    assert data["poe_port_num_mismatch"] is not None
    assert "exceed" in data["poe_port_num_mismatch"]


# ---- redaction through the server tools -------------------------------------


async def test_status_tool_envelope_has_no_password_or_cookie(tmp_path) -> None:
    cookie = "H_P_SSID=tplink_SECRETCOOKIE; Max-Age=600"
    fake = FakeSwitch().route(
        "GET",
        ROOT,
        httpx.Response(200, text=fixture("login_page.html"), headers={"set-cookie": cookie}),
    )
    backend = backend_for(tmp_path, fake, password="TopSecret99")
    mcp, _ = build_server(
        load_global_settings(MCP_ENV_PREFIX, {}), backend.settings, backend=backend
    )
    envelope = await _call(mcp, "switch_status")
    blob = json.dumps(envelope)
    assert envelope["success"] is True
    assert "TopSecret99" not in blob
    assert "SECRETCOOKIE" not in blob


async def test_login_tool_returns_hw_fw_and_logs_out(tmp_path) -> None:
    fake = (
        FakeSwitch()
        .route("GET", ROOT, fixture("login_page.html"))
        .route("POST", LOGON, fixture("logon_response_errtype0.html"))
        .route("GET", SYSINFO, fixture("system_info.html"))
        .route("GET", "/Logout.htm", fixture("login_page.html"))
    )
    backend = backend_for(tmp_path, fake, password="TopSecret99")
    mcp, _ = build_server(
        load_global_settings(MCP_ENV_PREFIX, {}), backend.settings, backend=backend
    )
    envelope = await _call(mcp, "switch_login")
    assert envelope["success"] is True
    assert envelope["data"]["hardware"] == "TL-SG1016PE 3.0"
    assert envelope["data"]["firmware"].startswith("1.0.0")
    assert fake.count("GET", "/Logout.htm") == 1
    assert "TopSecret99" not in json.dumps(envelope)


async def test_check_auth_tool_probes_without_login(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("login_page.html"))
    backend = backend_for(tmp_path, fake)
    mcp, _ = build_server(
        load_global_settings(MCP_ENV_PREFIX, {}), backend.settings, backend=backend
    )
    envelope = await _call(mcp, "switch_check_auth")
    assert envelope["success"] is True
    assert envelope["data"]["login_mode"] == "normal"
    assert fake.count("POST", LOGON) == 0


async def test_logout_tool_is_noop_when_not_logged_in(tmp_path) -> None:
    fake = FakeSwitch().route("GET", ROOT, fixture("login_page.html"))
    backend = backend_for(tmp_path, fake)
    mcp, _ = build_server(
        load_global_settings(MCP_ENV_PREFIX, {}), backend.settings, backend=backend
    )
    envelope = await _call(mcp, "switch_logout")
    assert envelope["success"] is True
    assert envelope["data"]["logged_out"] is True
    assert fake.calls == []  # no network when there is no session

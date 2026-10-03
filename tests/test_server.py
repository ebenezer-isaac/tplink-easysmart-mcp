"""Server build, switch_status, and the --list-tools / serve CLI (S1 scaffold)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tplink_easysmart_mcp import cli
from tplink_easysmart_mcp.core.config import DeviceSettings, load_global_settings
from tplink_easysmart_mcp.core.errors import ConfigError
from tplink_easysmart_mcp.server import MCP_ENV_PREFIX, build_server

EXPECTED_TOOLS = {"switch_status"}

PLACEHOLDER = {
    "env_prefix": "EASYSMART_",
    "host": "192.0.2.10",
    "password": "unused",
    "login_disabled": True,
}


def _device(**overrides: Any) -> DeviceSettings:
    return DeviceSettings.model_validate({**PLACEHOLDER, **overrides})


def _build(**overrides: Any):
    return build_server(load_global_settings(MCP_ENV_PREFIX, {}), _device(**overrides))


async def _call(mcp, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(name, args or {})
    structured = result[1] if isinstance(result, tuple) else result
    if isinstance(structured, dict) and "result" in structured and "success" not in structured:
        structured = structured["result"]
    if isinstance(structured, dict) and "success" in structured:
        return structured
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def test_build_registers_switch_status() -> None:
    mcp, names = _build()
    assert set(names) == EXPECTED_TOOLS
    assert {t.name for t in await mcp.list_tools()} == EXPECTED_TOOLS
    assert all(n.startswith("switch_") for n in names)


def test_list_tools_needs_no_configuration(monkeypatch) -> None:
    import os

    for key in [k for k in os.environ if k.startswith("EASYSMART_")]:
        monkeypatch.delenv(key)
    assert cli.list_tools() == ["switch_status"]


def test_cli_list_tools_prints_names(capsys) -> None:
    assert cli.main(["--list-tools"]) == 0
    assert capsys.readouterr().out.split() == ["switch_status"]


async def test_status_reports_policy_without_network() -> None:
    mcp, _ = _build(allow_writes=True, dry_run=True)
    status = await _call(mcp, "switch_status")
    assert status["success"] is True
    assert status["data"]["policy"] == {
        "writes_enabled": True,
        "dry_run": True,
        "login_disabled": True,
    }
    assert "version" in status["data"]


async def test_status_never_returns_password_or_cookie() -> None:
    mcp, _ = _build(password="TopSecret123")
    status = await _call(mcp, "switch_status")
    assert "TopSecret123" not in json.dumps(status)


def test_cli_serve_config_error_exit_code(monkeypatch, capsys) -> None:
    import os

    for key in [k for k in os.environ if k.startswith("EASYSMART_")]:
        monkeypatch.delenv(key)
    assert cli.main(["--env-file", "/nonexistent/.env"]) == 2
    assert "EASYSMART_HOST" in capsys.readouterr().err


def test_cli_serve_runs_selected_transport(monkeypatch, caplog) -> None:
    ran: list[str] = []
    monkeypatch.setattr(
        "mcp.server.fastmcp.FastMCP.run", lambda self, transport: ran.append(transport)
    )
    env = {
        "EASYSMART_HOST": "192.0.2.10",
        "EASYSMART_PASSWORD": "TestPass123",
        "EASYSMART_MCP_TRANSPORT": "streamable-http",
        "EASYSMART_MCP_HOST": "0.0.0.0",  # noqa: S104
    }
    cli.serve(env)
    assert ran == ["streamable-http"]
    assert "non-loopback" in caplog.text


def test_build_rejects_bad_global_settings() -> None:
    with pytest.raises(ConfigError):
        load_global_settings(MCP_ENV_PREFIX, {"EASYSMART_MCP_TRANSPORT": "sse"})

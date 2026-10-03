"""Server build, tool registration, and the --list-tools / serve / CLI plumbing."""

from __future__ import annotations

from typing import Any

import pytest

from tplink_easysmart_mcp import cli
from tplink_easysmart_mcp.core.config import load_global_settings
from tplink_easysmart_mcp.core.errors import ConfigError
from tplink_easysmart_mcp.server import MCP_ENV_PREFIX, build_server
from tplink_easysmart_mcp.switch.config import SwitchSettings

EXPECTED_TOOLS = {
    "switch_status",
    "switch_check_auth",
    "switch_login",
    "switch_logout",
    "switch_get_system_info",
    "switch_get_ports",
    "switch_get_port_stats",
    "switch_get_poe",
    "switch_get_vlans",
    "switch_resolve_port",
    "switch_set_poe",
    "switch_set_port",
    "switch_poe_cycle",
}

PLACEHOLDER = {
    "env_prefix": "EASYSMART_",
    "host": "192.0.2.10",
    "password": "unused",
    "login_disabled": True,
}


def _device(**overrides: Any) -> SwitchSettings:
    return SwitchSettings.model_validate({**PLACEHOLDER, **overrides})


def _build(**overrides: Any):
    return build_server(load_global_settings(MCP_ENV_PREFIX, {}), _device(**overrides))


async def test_build_registers_all_tools() -> None:
    mcp, names = _build()
    assert set(names) == EXPECTED_TOOLS
    assert {t.name for t in await mcp.list_tools()} == EXPECTED_TOOLS
    assert all(n.startswith("switch_") for n in names)


def test_list_tools_needs_no_configuration(monkeypatch) -> None:
    import os

    for key in [k for k in os.environ if k.startswith("EASYSMART_")]:
        monkeypatch.delenv(key)
    assert cli.list_tools() == sorted(EXPECTED_TOOLS)


def test_cli_list_tools_prints_names(capsys) -> None:
    assert cli.main(["--list-tools"]) == 0
    assert capsys.readouterr().out.split() == sorted(EXPECTED_TOOLS)


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


def _clean_env(monkeypatch, tmp_path) -> None:
    import os

    for key in [k for k in os.environ if k.startswith("EASYSMART_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("EASYSMART_HOST", "192.0.2.10")
    monkeypatch.setenv("EASYSMART_PASSWORD", "TestPass123")
    monkeypatch.setenv("EASYSMART_STATE_DIR", str(tmp_path))


def test_cli_breaker_show_and_clear(monkeypatch, tmp_path, capsys) -> None:
    _clean_env(monkeypatch, tmp_path)
    assert cli.main(["breaker", "--show"]) == 0
    assert '"state": "closed"' in capsys.readouterr().out
    assert cli.main(["breaker", "--clear"]) == 0
    assert "cleared" in capsys.readouterr().out.lower()


def test_cli_check_auth_uses_backend(monkeypatch, tmp_path, capsys) -> None:
    _clean_env(monkeypatch, tmp_path)

    class FakeBackend:
        def __init__(self, device: Any) -> None:
            self.device = device

        async def check_auth(self) -> dict:
            return {"reachable": True, "session_model": "ip_bound"}

        async def login_once(self) -> dict:
            return {"logged_in": True, "hardware": "TL-SG1016PE 3.0"}

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(cli, "EasySmartSwitchBackend", FakeBackend)
    assert cli.main(["check-auth"]) == 0
    assert '"session_model": "ip_bound"' in capsys.readouterr().out
    assert cli.main(["check-auth", "--login"]) == 0
    assert '"logged_in": true' in capsys.readouterr().out

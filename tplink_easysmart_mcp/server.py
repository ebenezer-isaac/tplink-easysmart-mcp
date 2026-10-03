"""Build the FastMCP app for one Easy Smart switch.

Registers the three S2 tools, all prefixed ``switch_``:

* ``switch_status`` — healthcheck: config summary + one credential-free
  reachability GET + session-model guess + breaker/cooldown state. No login.
* ``switch_check_auth`` — the probe only (no login, no POST).
* ``switch_login`` — one explicit login, confirm, then logout. Returns the session
  model and hw/fw. Never returns the password or any cookie.

Every tool returns the ``{success, data, error}`` envelope through ``run_tool``,
which also recursively redacts credential-bearing fields.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .core.config import GlobalSettings
from .core.tooling import run_tool
from .switch.backend import EasySmartSwitchBackend
from .switch.config import SwitchSettings

MCP_ENV_PREFIX = "EASYSMART_MCP_"
STATUS_TOOL = "switch_status"
CHECK_AUTH_TOOL = "switch_check_auth"
LOGIN_TOOL = "switch_login"
LOGOUT_TOOL = "switch_logout"

INSTRUCTIONS = (
    "Tools for one local TP-Link Easy Smart switch, all prefixed switch_. Call "
    "switch_status first (it never logs in). switch_check_auth probes without "
    "logging in. switch_login performs exactly one login then logs out; "
    "switch_logout ends any lingering session. Never retry a failed login: the "
    "switch locks the admin account, a persistent breaker blocks further logins "
    "until a human clears it, and logging in evicts the owner's web-UI session "
    "(and vice versa)."
)


def build_server(
    settings: GlobalSettings,
    device: SwitchSettings,
    *,
    backend: EasySmartSwitchBackend | None = None,
) -> tuple[FastMCP, list[str]]:
    """Return the app and the names of every tool it registers."""
    mcp = FastMCP(
        name="tplink-easysmart-mcp",
        instructions=INSTRUCTIONS,
        host=settings.mcp_host,
        port=settings.mcp_port,
    )
    device_backend = backend if backend is not None else EasySmartSwitchBackend(device)

    @mcp.tool(name=STATUS_TOOL)
    async def _status() -> dict:
        """Healthcheck: policy, one credential-free reachability GET, session-model
        guess and breaker state. Never logs in, never POSTs, never returns the
        password or cookies."""
        return await run_tool(STATUS_TOOL, device_backend.healthcheck)

    @mcp.tool(name=CHECK_AUTH_TOOL)
    async def _check_auth() -> dict:
        """Probe the switch without logging in: report the session model, auth
        variant and login mode plus breaker/cooldown state. Makes one GET, no POST."""
        return await run_tool(CHECK_AUTH_TOOL, device_backend.check_auth)

    @mcp.tool(name=LOGIN_TOOL)
    async def _login() -> dict:
        """Perform exactly one login, confirm it with a data GET, then log out.
        Returns the session model and hardware/firmware. Never returns cookies.
        Refuses (no POST) in restored-account or encrypted-variant modes, and the
        breaker blocks it after a prior failure until a human clears it."""
        return await run_tool(LOGIN_TOOL, device_backend.login_once)

    @mcp.tool(name=LOGOUT_TOOL)
    async def _logout() -> dict:
        """End any lingering session with GET /Logout.htm. A no-op (no network) if
        not currently logged in. Does not mutate switch settings."""
        return await run_tool(LOGOUT_TOOL, device_backend.logout)

    return mcp, [STATUS_TOOL, CHECK_AUTH_TOOL, LOGIN_TOOL, LOGOUT_TOOL]

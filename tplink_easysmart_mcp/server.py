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
from .core.types import ConfirmWrite
from .core.write_gate import check_write_gate
from .switch.backend import EasySmartSwitchBackend
from .switch.config import SwitchSettings
from .switch.cycle import poe_cycle_op
from .switch.tools_read import (
    get_poe_op,
    get_port_stats_op,
    get_ports_op,
    get_system_info_op,
    get_vlans_op,
    resolve_port_op,
)
from .switch.tools_write import set_poe_op, set_port_op

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

    # ---- read tools (no write, no logout unless EASYSMART_LOGOUT_AFTER_READS) ----

    @mcp.tool(name="switch_get_system_info")
    async def _get_system_info() -> dict:
        """Read-only: model, hardware revision, firmware, MAC, IP, netmask, gateway
        and the session model. Logs in lazily; does not mutate anything."""
        return await run_tool("switch_get_system_info", lambda: get_system_info_op(device_backend))

    @mcp.tool(name="switch_get_ports")
    async def _get_ports(only_linked: bool = False) -> dict:
        """Read-only: per-port admin state, link, configured/actual speed, flow
        control, LAG id, friendly name and whether the port is protected. Set
        only_linked=true to list only ports with a live link. Does not mutate."""
        return await run_tool(
            "switch_get_ports", lambda: get_ports_op(device_backend, only_linked=only_linked)
        )

    @mcp.tool(name="switch_get_port_stats")
    async def _get_port_stats(port: int | str | None = None) -> dict:
        """Read-only: tx/rx good/bad packet counters. Pass a port number or a
        PORT_MAP name for one port, or omit it for all ports plus error_ports
        (any port with rx_bad+tx_bad > 0). Does not mutate."""
        return await run_tool(
            "switch_get_port_stats", lambda: get_port_stats_op(device_backend, port=port)
        )

    @mcp.tool(name="switch_get_poe")
    async def _get_poe() -> dict:
        """Read-only: PoE budget totals and per-port state, priority, power limit,
        PD class ("--" when none), watts, milliamps, volts and status, plus derived
        fault_ports and unpowered_enabled_ports. Does not mutate."""
        return await run_tool("switch_get_poe", lambda: get_poe_op(device_backend))

    @mcp.tool(name="switch_get_vlans")
    async def _get_vlans() -> dict:
        """Read-only: the 802.1Q VLAN table and per-port PVIDs. Returns NOT_SUPPORTED
        if this firmware does not serve a VLAN page. Does not mutate."""
        return await run_tool("switch_get_vlans", lambda: get_vlans_op(device_backend))

    @mcp.tool(name="switch_resolve_port")
    async def _resolve_port(name_or_port: int | str) -> dict:
        """Read-only helper: resolve a port number or PORT_MAP name to {port, name,
        is_poe, protected, max_port}. Resolution is type-stable: an integer or a
        string of digits is ALWAYS a port number (never a name), so 3 and "3" mean
        the same physical port and a numeric name can never shadow one. A non-digit
        string is a case-insensitive name. UNKNOWN_PORT lists the known names;
        AMBIGUOUS_NAME if a name maps to more than one port; an out-of-range number
        is INVALID_PORT. Does not mutate."""
        return await run_tool(
            "switch_resolve_port",
            lambda: resolve_port_op(device_backend, name_or_port=name_or_port),
        )

    # ---- write tools: both gates checked here, before any network call ----------

    @mcp.tool(name="switch_set_poe")
    async def _set_poe(port: int | str, enabled: bool, confirm_write: ConfirmWrite = False) -> dict:
        """MUTATES PoE on one port. Reads the live PoE page, re-sends the port's
        current priority and power limit, and changes only on/off, then verifies
        (WRITE_VERIFY_FAILED if priority/limit were clobbered). Refuses a non-PoE
        port (NOT_POE_PORT) or a protected port (PROTECTED_PORT). Requires
        EASYSMART_ALLOW_WRITES=true and confirm_write=true; otherwise no network
        call. Honours EASYSMART_DRY_RUN (returns the exact form, sends nothing)."""
        refusal = check_write_gate(device_backend.settings, "post", confirm_write)
        if refusal is not None:
            return refusal
        return await run_tool(
            "switch_set_poe", lambda: set_poe_op(device_backend, port=port, enabled=enabled)
        )

    @mcp.tool(name="switch_set_port")
    async def _set_port(
        port: int | str, enabled: bool, confirm_write: ConfirmWrite = False
    ) -> dict:
        """MUTATES one port's admin state (enable/disable the link). Reads the live
        page, re-sends the port's current speed and flow control, changes only the
        state, then verifies. Refuses a protected port (PROTECTED_PORT). Requires
        both write gates; otherwise no network call. Honours EASYSMART_DRY_RUN."""
        refusal = check_write_gate(device_backend.settings, "post", confirm_write)
        if refusal is not None:
            return refusal
        return await run_tool(
            "switch_set_port", lambda: set_port_op(device_backend, port=port, enabled=enabled)
        )

    @mcp.tool(name="switch_poe_cycle")
    async def _poe_cycle(
        port_or_name: int | str, off_seconds: int = 10, confirm_write: ConfirmWrite = False
    ) -> dict:
        """MUTATES: power-cycle one PoE camera (off, wait off_seconds, on, wait for
        power). Resolve a PORT_MAP camera name or port number. Refuses non-PoE
        (NOT_POE_PORT), protected (PROTECTED_PORT), an already-off port (ALREADY_OFF)
        and a second concurrent cycle (CYCLE_IN_PROGRESS). If it cannot restore
        power it reports CYCLE_INCOMPLETE/POWER_NOT_RESTORED naming the port that may
        be UNPOWERED. Requires both write gates; honours EASYSMART_DRY_RUN (returns
        both planned forms, sends nothing)."""
        refusal = check_write_gate(device_backend.settings, "post", confirm_write)
        if refusal is not None:
            return refusal
        return await run_tool(
            "switch_poe_cycle",
            lambda: poe_cycle_op(
                device_backend, port_or_name=port_or_name, off_seconds=off_seconds
            ),
        )

    return mcp, [
        STATUS_TOOL,
        CHECK_AUTH_TOOL,
        LOGIN_TOOL,
        LOGOUT_TOOL,
        "switch_get_system_info",
        "switch_get_ports",
        "switch_get_port_stats",
        "switch_get_poe",
        "switch_get_vlans",
        "switch_resolve_port",
        "switch_set_poe",
        "switch_set_port",
        "switch_poe_cycle",
    ]

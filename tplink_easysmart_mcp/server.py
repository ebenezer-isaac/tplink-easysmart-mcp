"""Build the FastMCP app for one Easy Smart switch.

Phase S1 scaffold: this registers only ``switch_status`` (a networkless
healthcheck). Phases S2/S3 add the authenticated and typed tools and enrich the
status payload with the live probe. The status tool never contacts the switch
and never returns the password or any cookie.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .core.config import DeviceSettings, GlobalSettings
from .core.envelope import ok

MCP_ENV_PREFIX = "EASYSMART_MCP_"
STATUS_TOOL = "switch_status"

INSTRUCTIONS = (
    "Tools for one local TP-Link Easy Smart switch, all prefixed switch_. Call "
    "switch_status first. Reads are safe. Writes need the server's "
    "EASYSMART_ALLOW_WRITES=true AND confirm_write=true on the call; confirm with "
    "the operator first. Never retry a failed login: the switch locks the admin "
    "account, and logging in evicts the owner's web-UI session (and vice versa)."
)


def build_server(settings: GlobalSettings, device: DeviceSettings) -> tuple[FastMCP, list[str]]:
    """Return the app and the names of every tool it registers."""
    mcp = FastMCP(
        name="tplink-easysmart-mcp",
        instructions=INSTRUCTIONS,
        host=settings.mcp_host,
        port=settings.mcp_port,
    )

    @mcp.tool(name=STATUS_TOOL)
    async def _status() -> dict[str, Any]:
        """Healthcheck: server version and the safety policy in force. Never logs
        in, never contacts the switch, and never returns the password or cookies."""
        return ok(
            {
                "version": __version__,
                "policy": {
                    "writes_enabled": device.allow_writes,
                    "dry_run": device.dry_run,
                    "login_disabled": device.login_disabled,
                },
            }
        )

    return mcp, [STATUS_TOOL]

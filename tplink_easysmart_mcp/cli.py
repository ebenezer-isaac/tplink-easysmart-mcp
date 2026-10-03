"""Command line: ``tplink-easysmart-mcp`` / ``python -m tplink_easysmart_mcp``.

tplink-easysmart-mcp               # serve (EASYSMART_MCP_TRANSPORT: stdio | streamable-http)
tplink-easysmart-mcp --list-tools  # print registered tool names; no device I/O

Phase S1 scaffold. The authenticated ``check-auth`` / ``breaker`` subcommands and
the typed tools arrive in later phases; this entrypoint already serves the
``switch_status`` healthcheck so the release gate can enumerate tools.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence

from dotenv import load_dotenv

from .core.cli import configure_logging, emit_lines
from .core.config import DeviceSettings, load_device_settings, load_global_settings
from .core.errors import ConfigError
from .server import MCP_ENV_PREFIX, build_server

log = logging.getLogger("tplink_easysmart_mcp")

ENV_PREFIX = "EASYSMART_"

# Placeholder device used only by --list-tools; never contacted.
_LISTING_SETTINGS = {
    "env_prefix": ENV_PREFIX,
    "host": "192.0.2.1",
    "password": "unused",
    "login_disabled": True,
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tplink-easysmart-mcp",
        description="Local, browser-free MCP server for a TP-Link Easy Smart switch.",
    )
    parser.add_argument(
        "--list-tools", action="store_true", help="print registered tool names and exit"
    )
    parser.add_argument("--env-file", default=None, help="load variables from this .env file")
    return parser.parse_args(argv)


def list_tools() -> list[str]:
    device = DeviceSettings.model_validate(_LISTING_SETTINGS)
    _, names = build_server(load_global_settings(MCP_ENV_PREFIX, {}), device)
    return sorted(names)


def serve(environ: Mapping[str, str]) -> None:
    settings = load_global_settings(MCP_ENV_PREFIX, environ)
    device = load_device_settings(ENV_PREFIX, environ)
    if settings.mcp_transport == "streamable-http" and not settings.mcp_host_is_loopback:
        log.warning(
            "binding MCP HTTP to non-loopback %s; this server has no auth of its own. "
            "Prefer 127.0.0.1 behind Tailscale or an SSH tunnel.",
            settings.mcp_host,
        )
    mcp, names = build_server(settings, device)
    log.info("serving %d tools over %s", len(names), settings.mcp_transport)
    mcp.run(transport=settings.mcp_transport)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_tools:
        return emit_lines(list_tools())
    load_dotenv(args.env_file, override=False)
    configure_logging("EASYSMART_MCP_LOG_LEVEL")
    try:
        serve(os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    return 0

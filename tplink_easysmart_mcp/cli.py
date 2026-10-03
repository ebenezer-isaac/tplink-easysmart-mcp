"""Command line: ``tplink-easysmart-mcp`` / ``python -m tplink_easysmart_mcp``.

tplink-easysmart-mcp                      # serve (EASYSMART_MCP_TRANSPORT)
tplink-easysmart-mcp --list-tools         # print tool names; no device I/O
tplink-easysmart-mcp check-auth [--login] # probe (one GET); --login does one login+logout
tplink-easysmart-mcp breaker --show       # print the persistent breaker state; no I/O
tplink-easysmart-mcp breaker --clear      # clear the breaker after fixing the cause
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence

from dotenv import load_dotenv

from .core.cli import configure_logging, emit_envelope, emit_lines, run_breaker
from .core.config import load_global_settings
from .core.errors import ConfigError
from .core.tooling import run_tool
from .server import MCP_ENV_PREFIX, build_server
from .switch.backend import EasySmartSwitchBackend
from .switch.config import SwitchSettings, load_switch_settings

log = logging.getLogger("tplink_easysmart_mcp")

# Placeholder device used only by --list-tools; never contacted. (6-char password
# satisfies the switch's 6-16 bound; login is disabled so no auth can run.)
_LISTING_SETTINGS = {
    "env_prefix": "EASYSMART_",
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
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the MCP server (the default)")

    check = sub.add_parser("check-auth", help="probe the switch (optionally log in once)")
    check.add_argument(
        "--login", action="store_true", help="perform exactly one login then log out"
    )

    brk = sub.add_parser("breaker", help="show or clear the persistent login breaker")
    group = brk.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true", help="print the breaker state")
    group.add_argument("--clear", action="store_true", help="clear the breaker")

    return parser.parse_args(argv)


def list_tools() -> list[str]:
    device = SwitchSettings.model_validate(_LISTING_SETTINGS)
    _, names = build_server(load_global_settings(MCP_ENV_PREFIX, {}), device)
    return sorted(names)


def serve(environ: Mapping[str, str]) -> None:
    settings = load_global_settings(MCP_ENV_PREFIX, environ)
    device = load_switch_settings(dict(environ))
    if settings.mcp_transport == "streamable-http" and not settings.mcp_host_is_loopback:
        log.warning(
            "binding MCP HTTP to non-loopback %s; this server has no auth of its own. "
            "Prefer 127.0.0.1 behind Tailscale or an SSH tunnel.",
            settings.mcp_host,
        )
    mcp, names = build_server(settings, device)
    log.info("serving %d tools over %s", len(names), settings.mcp_transport)
    mcp.run(transport=settings.mcp_transport)


def _check_auth(environ: Mapping[str, str], *, login: bool) -> int:
    device = load_switch_settings(dict(environ))
    backend = EasySmartSwitchBackend(device)
    name = "switch_login" if login else "switch_check_auth"
    op = backend.login_once if login else backend.check_auth

    async def _run() -> dict:
        try:
            return await run_tool(name, op)
        finally:
            await backend.aclose()

    return emit_envelope(asyncio.run(_run()))


def _breaker(environ: Mapping[str, str], *, clear: bool) -> int:
    device = load_switch_settings(dict(environ))
    backend = EasySmartSwitchBackend(device)
    return run_breaker(backend.breaker, "clear" if clear else "show")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_tools:
        return emit_lines(list_tools())
    load_dotenv(args.env_file, override=False)
    configure_logging("EASYSMART_MCP_LOG_LEVEL")
    try:
        if args.command == "check-auth":
            return _check_auth(os.environ, login=args.login)
        if args.command == "breaker":
            return _breaker(os.environ, clear=args.clear)
        serve(os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    return 0

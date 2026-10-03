"""The single switch backend wired into the MCP server and CLI.

Holds the one ``SwitchClient`` (created lazily so ``--list-tools`` never opens a
socket), the persistent breaker and the persistent cooldown, and exposes the
three S2 operations:

* ``healthcheck`` — the probe only (one credential-free GET, no login, no POST),
  plus the config summary and breaker/cooldown state. Backs ``switch_status``.
* ``check_auth`` — the probe only. Backs ``switch_check_auth`` and ``check-auth``.
* ``login_once`` — exactly one login, confirm, then logout. Backs ``switch_login``
  and ``check-auth --login``. Returns the session model and hw/fw, never cookies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .. import __version__
from ..core.breaker import Clock, LoginBreaker
from ..core.errors import TransportError
from .auth import LoginCooldown
from .client import SwitchClient
from .config import SwitchSettings, poe_ports_mismatch
from .cycle import CycleMarker

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class EasySmartSwitchBackend:
    """The only backend the server registers."""

    def __init__(
        self,
        settings: SwitchSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Clock = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._breaker = LoginBreaker(
            settings.state_path,
            settings.host,
            clear_hint="run `tplink-easysmart-mcp breaker --clear` once the cause is fixed",
        )
        self._cooldown = LoginCooldown(
            settings.state_path, settings.host, settings.login_cooldown_s, now=now
        )
        self._cycle_marker = CycleMarker(settings.state_path, settings.host, now=now)
        self._cycle_lock = asyncio.Lock()
        self._client: SwitchClient | None = None
        self._poe_warning: str | None = None

    @property
    def now(self) -> Clock:
        return self._now

    @property
    def sleep(self) -> Sleep:
        return self._sleep

    @property
    def cycle_lock(self) -> asyncio.Lock:
        return self._cycle_lock

    @property
    def cycle_marker(self) -> CycleMarker:
        return self._cycle_marker

    @property
    def settings(self) -> SwitchSettings:
        return self._settings

    @property
    def breaker(self) -> LoginBreaker:
        return self._breaker

    @property
    def cooldown(self) -> LoginCooldown:
        return self._cooldown

    @property
    def client(self) -> SwitchClient:
        if self._client is None:
            self._client = SwitchClient(
                self._settings, self._breaker, self._cooldown, transport=self._transport
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def observe_poe_port_num(self, poe_port_num: int) -> str | None:
        """Record any POE_PORTS/poe_port_num mismatch so ``switch_status`` can report it."""
        self._poe_warning = poe_ports_mismatch(self._settings, poe_port_num)
        if self._poe_warning:
            log.warning("PoE port configuration mismatch: %s", self._poe_warning)
        return self._poe_warning

    async def healthcheck(self) -> dict[str, Any]:
        """``switch_status`` payload: policy + one reachability probe + breaker state."""
        data: dict[str, Any] = {
            "version": __version__,
            "config": self._settings.summary(),
            "breaker": self._breaker.state().snapshot(),
            "cooldown": self._cooldown.snapshot(),
            "cycle": self._cycle_marker.snapshot(),
            "poe_port_num_mismatch": self._poe_warning,
        }
        try:
            probe = await self.client.probe()
        except TransportError as exc:
            data["reachable"] = False
            data["probe_error"] = str(exc)
            return data
        data["reachable"] = True
        data["session_model"] = probe.session_model.value
        data["auth_variant"] = probe.auth_variant.value
        data["login_mode"] = probe.login_mode.value
        data["err_type"] = probe.err_type
        return data

    async def check_auth(self) -> dict[str, Any]:
        """Probe only: report the session model, auth variant and login mode. No login."""
        probe = await self.client.probe()
        return {
            "reachable": True,
            "session_model": probe.session_model.value,
            "auth_variant": probe.auth_variant.value,
            "login_mode": probe.login_mode.value,
            "err_type": probe.err_type,
            "breaker": self._breaker.state().snapshot(),
            "cooldown": self._cooldown.snapshot(),
        }

    async def logout(self) -> dict[str, Any]:
        """Ensure the current session is logged out. A no-op (no I/O) if not logged in."""
        await self.client.logout()
        return {"logged_out": True, "authenticated": self.client.authenticated}

    async def login_once(self) -> dict[str, Any]:
        """One explicit login, confirm, then logout. Returns session model and hw/fw."""
        client = self.client
        try:
            result = await client.login()
        finally:
            await client.logout()
        info = result.system_info
        return {
            "logged_in": True,
            "session_model": result.session_model.value,
            "auth_variant": result.auth_variant.value,
            "model": info.name,
            "hardware": info.hardware,
            "hw_revision": info.hw_revision,
            "firmware": info.firmware,
        }

"""The single switch backend wired into the MCP server and CLI.

Holds the one ``SwitchClient`` (created lazily so ``--list-tools`` never opens a
socket) and the one persistent :class:`LoginBreaker` (which now owns the cooldown,
folded from the old separate ``LoginCooldown`` file — X1b core sync), and exposes
the three S2 operations:

* ``healthcheck`` — the probe only (one credential-free GET, no login, no POST),
  plus the config summary and breaker/cycle state. Backs ``switch_status``.
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
from ..core.breaker import LoginBreaker, canonical_device_key
from ..core.errors import TransportError
from .client import SwitchClient
from .config import SwitchSettings, poe_ports_mismatch
from .cycle_guard import CycleGuard

log = logging.getLogger(__name__)

Clock = Callable[[], float]
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
        device_key = canonical_device_key(settings.host)
        self._breaker = LoginBreaker(
            settings.state_path,
            device_key,
            max_failures=settings.max_login_failures,
            clock=now,
            login_disabled=settings.login_disabled,
            disabled_hint=f"{settings.env_name('LOGIN_DISABLED')}=true freezes auth",
        )
        self._cycle_guard = CycleGuard(settings.state_path, device_key, now=now)
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
    def cycle_guard(self) -> CycleGuard:
        return self._cycle_guard

    @property
    def settings(self) -> SwitchSettings:
        return self._settings

    @property
    def breaker(self) -> LoginBreaker:
        return self._breaker

    @property
    def client(self) -> SwitchClient:
        if self._client is None:
            self._client = SwitchClient(self._settings, self._breaker, transport=self._transport)
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
            "breaker": self._breaker.status(),
            "cycle": self._cycle_guard.status(),
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
            "breaker": self._breaker.status(),
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

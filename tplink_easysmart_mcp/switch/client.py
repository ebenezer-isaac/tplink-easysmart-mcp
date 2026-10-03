"""The networked Easy Smart switch client: serialised I/O, lazy login, one re-login.

Wraps a single ``httpx.AsyncClient`` (with a cookie jar, a ``Referer`` header and a
short timeout) behind a per-device serial lock. Reads log in lazily; a data page
that comes back as the login page triggers **exactly one** re-login and re-GET,
and a second login page is a hard ``SESSION_LOST``. Mutating submits honour
dry-run (sending nothing and returning the redacted plan) and turn a connection
reset into ``OUTCOME_UNKNOWN`` — the caller re-reads, it never resends.

The copied ``core.transport`` speaks JSON only and cannot carry this HTML/form
protocol, so the client talks to ``httpx`` directly while keeping the same
guarantees: the ``logon.cgi`` body is never logged, and cookie values never leave
this module.

Request logic is ported from ``vmakeev/hass_tplink_easy_smart`` (MIT, (c) 2022
Vladimir Makeev); none of its files are copied.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx

from ..core.breaker import LoginBreaker
from ..core.errors import TransportError
from ..core.serial import SerialLock
from .auth import ConnectionResetSignal, HttpResult, LoginCooldown, LoginResult, SwitchAuthenticator
from .config import SwitchSettings
from .constants import (
    ANCHOR_POE,
    ANCHOR_PORT_STATS,
    ANCHOR_PORTS,
    ANCHOR_SYSTEM_INFO,
    ANCHOR_VLAN,
    POE_CONFIG,
    PORT_SETTING,
    PORT_STATISTICS,
    SYSTEM_INFO,
    VLAN_8021Q,
)
from .errors import SessionLost
from .forms import RequestPlan, plan_redacted
from .models import (
    PageClass,
    PoeSnapshot,
    PortState,
    PortStats,
    SystemInfo,
    VlanTable,
)
from .pages import classify
from .parsers import (
    parse_poe,
    parse_port_stats,
    parse_ports,
    parse_system_info,
    parse_vlans,
)

log = logging.getLogger(__name__)

MAX_PAGE_BYTES = 1_000_000
_RESET_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError)


class SwitchClient:
    """One switch, one in-flight request at a time."""

    def __init__(
        self,
        settings: SwitchSettings,
        breaker: LoginBreaker,
        cooldown: LoginCooldown,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={"Referer": settings.referer},
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )
        self._lock = SerialLock()
        self._auth = SwitchAuthenticator(settings, self, breaker, cooldown)

    # -- transport surface used by the authenticator --------------------------

    def clear_cookies(self) -> None:
        self._client.cookies.clear()

    async def raw_get(self, path: str) -> HttpResult:
        return await self._do("GET", path, content=None, mutating=False)

    async def raw_post(self, path: str, fields: tuple[tuple[str, str], ...]) -> HttpResult:
        body = urlencode(fields).encode("utf-8")
        return await self._do("POST", path, content=body, mutating=True)

    async def _do(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None,
        mutating: bool,
    ) -> HttpResult:
        # The body is never logged (it carries the logon.cgi password); only the path is.
        log.debug("%s %s%s", method, path, " (body omitted)" if content else "")
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if content else None
        try:
            response = await self._client.request(method, path, content=content, headers=headers)
        except _RESET_ERRORS as exc:
            if mutating:
                raise ConnectionResetSignal(type(exc).__name__) from exc
            raise TransportError(f"{method} {path} failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise TransportError(f"{method} {path} returned HTTP {response.status_code}")
        if len(response.content) > MAX_PAGE_BYTES:
            raise TransportError(f"{method} {path} response exceeds size limit")
        return HttpResult(response.text, response.headers)

    # -- lifecycle ------------------------------------------------------------

    @property
    def authenticator(self) -> SwitchAuthenticator:
        return self._auth

    @property
    def authenticated(self) -> bool:
        return self._auth.authenticated

    async def probe(self):
        return await self._lock.run(self._auth.probe)

    async def login(self) -> LoginResult:
        return await self._lock.run(self._auth.login)

    async def logout(self) -> None:
        await self._lock.run(self._auth.logout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SwitchClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            await self._lock.run(self._auth.logout)
        finally:
            await self._client.aclose()

    @asynccontextmanager
    async def session_scope(self, logout_after: bool) -> AsyncIterator[SwitchClient]:
        """For mutating tools: always ``GET /Logout.htm`` in ``finally`` if asked."""
        try:
            yield self
        finally:
            if logout_after:
                await self.logout()

    # -- page access ----------------------------------------------------------

    async def get_page(self, path: str, anchor: str) -> str:
        return await self._lock.run(lambda: self._get_page_impl(path, anchor))

    async def _get_page_impl(self, path: str, anchor: str) -> str:
        await self._ensure_login()
        result = await self.raw_get(path)
        if classify(result.text, anchor) is not PageClass.LOGIN_PAGE:
            return result.text
        # The session was evicted (the owner logged into the web UI, or it timed
        # out). Re-login exactly once, then try once more.
        self._auth.mark_logged_out()
        await self._auth.login()
        result = await self.raw_get(path)
        if classify(result.text, anchor) is PageClass.LOGIN_PAGE:
            raise SessionLost(
                f"the {path} page returned the login page twice; a single re-login did not recover"
            )
        return result.text

    async def submit(self, plan: RequestPlan) -> RequestPlan | None:
        return await self._lock.run(lambda: self._submit_impl(plan))

    async def _submit_impl(self, plan: RequestPlan) -> RequestPlan | None:
        if self._settings.dry_run:
            # Dry-run sends nothing at all, not even a login, and shows the bytes.
            return plan_redacted(plan)
        await self._ensure_login()
        try:
            if plan.method == "GET":
                query = urlencode(plan.fields)
                await self._do("GET", f"{plan.path}?{query}", content=None, mutating=True)
            else:
                await self.raw_post(plan.path, plan.fields)
        except ConnectionResetSignal as exc:
            from .errors import OutcomeUnknown

            raise OutcomeUnknown(
                f"the {plan.path} request's connection was reset ({exc}); the outcome is unknown. "
                "Re-read the page to settle it; do not resend."
            ) from None
        return None

    async def _ensure_login(self) -> None:
        if not self._auth.authenticated:
            await self._auth.login()

    # -- typed readers --------------------------------------------------------

    async def system_info(self) -> SystemInfo:
        return parse_system_info(await self.get_page(SYSTEM_INFO, ANCHOR_SYSTEM_INFO))

    async def ports(self) -> tuple[PortState, ...]:
        return parse_ports(await self.get_page(PORT_SETTING, ANCHOR_PORTS))

    async def port_stats(self) -> tuple[PortStats, ...]:
        return parse_port_stats(await self.get_page(PORT_STATISTICS, ANCHOR_PORT_STATS))

    async def poe(self) -> PoeSnapshot:
        return parse_poe(await self.get_page(POE_CONFIG, ANCHOR_POE))

    async def vlans(self) -> VlanTable:
        return parse_vlans(await self.get_page(VLAN_8021Q, ANCHOR_VLAN))

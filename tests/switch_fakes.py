"""An in-process fake Easy Smart switch (httpx.MockTransport) for S2 client tests.

No real device is contacted. Each route is a list of responders consumed in order
(the last one repeats), where a responder is a fixture name, an ``httpx.Response``,
an ``Exception`` to raise (to simulate a connection reset), or a callable taking
the request. Every request is recorded so tests can assert call counts and that a
POST carried the session cookie.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from tplink_easysmart_mcp.switch.config import SwitchSettings

FIXTURES = Path(__file__).resolve().parent / "fixtures"

Responder = str | httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def login_page_with_cookie(
    cookie: str = "H_P_SSID=tplink_abc123; Max-Age=600; Path=/",
) -> httpx.Response:
    return httpx.Response(200, text=fixture("login_page.html"), headers={"set-cookie": cookie})


class FakeSwitch:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], list[Responder]] = {}

    def route(self, method: str, path: str, *responders: Responder) -> FakeSwitch:
        self._routes[(method, path)] = list(responders)
        return self

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.calls if r.method == method and r.url.path == path)

    def request_for(self, method: str, path: str) -> httpx.Request | None:
        for r in self.calls:
            if r.method == method and r.url.path == path:
                return r
        return None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        responders = self._routes.get((request.method, request.url.path))
        if not responders:
            return httpx.Response(404, text="no route configured")
        responder = responders.pop(0) if len(responders) > 1 else responders[0]
        return _materialise(responder, request)


def _materialise(responder: Responder, request: httpx.Request) -> httpx.Response:
    if isinstance(responder, Exception):
        raise responder
    if isinstance(responder, httpx.Response):
        return responder
    if isinstance(responder, str):
        return httpx.Response(200, text=responder)
    return responder(request)


def make_settings(tmp_path: Path, **overrides: object) -> SwitchSettings:
    base: dict[str, object] = {
        "env_prefix": "EASYSMART_",
        "host": "192.0.2.10",
        "password": "TestPass123",
        "state_dir": str(tmp_path),
    }
    base.update(overrides)
    return SwitchSettings.model_validate(base)

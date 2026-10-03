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
from urllib.parse import parse_qsl

import httpx

from tplink_easysmart_mcp.switch.backend import EasySmartSwitchBackend
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


# --------------------------------------------------------------------------- #
# A stateful in-process fake switch for the S3 tool tests.
#
# Unlike the route-list FakeSwitch above, this one holds live PoE/port state,
# renders each *Rpm.htm page from it, and applies every *.cgi write so a tool can
# do a real read-modify-write and a re-read sees the change. Knobs simulate the
# adversarial paths: session eviction, a clobbering write, a swallowed "on", a
# PD that never draws power, a connection reset, and a login that now fails.
# --------------------------------------------------------------------------- #

_ERRTYPE_FIXTURE = {
    0: "logon_response_errtype0.html",
    1: "logon_response_errtype1_bad_credentials.html",
    2: "logon_response_errtype2_user_blocked.html",
    4: "logon_response_errtype4_sessions_full.html",
    6: "logon_response_errtype6_password_change.html",
}


class StatefulSwitch:
    """A fake TL-SG1016PE that serves every page from state and applies writes."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.logins = 0
        self.device_authed = False
        self.login_errtype = 0
        # knobs
        self.clobber_poe_priority = False
        self.clobber_port_speed = False
        self.ignore_poe_on = 0  # swallow this many upcoming "turn on" writes
        self.power_restores = True  # a re-enabled port draws power (status on)
        self.reset_on_poe_write = 0  # raise a connection reset on the next N PoE writes
        self.vlan_unexpected = False  # serve a non-VLAN body for the VLAN page
        # PoE state (8 ports), seeded from poe_config.html
        self.poe: dict[str, list[int]] = {
            "state": [1, 1, 1, 1, 1, 1, 1, 0],
            "priority": [0, 1, 2, 2, 2, 2, 2, 0],
            "powerlimit": [330, 330, 154, 330, 255, 330, 300, 330],
            "power": [41, 62, 128, 0, 89, 0, 0, 0],
            "current": [79, 119, 245, 0, 171, 0, 0, 0],
            "voltage": [523, 522, 521, 0, 520, 0, 0, 0],
            "pdclass": [330, 70, 154, 0, 300, 0, 300, 0],
            "powerstatus": [2, 2, 2, 0, 2, 5, 3, 0],
        }
        self.poe_port_num = 8
        self.poe_global = [400, 320, 80, 10, 1100]
        # Port state (16 ports), seeded from port_setting.html
        self.port: dict[str, list[int]] = {
            "state": [1] * 11 + [0] + [1] * 4,
            "trunk_info": [0] * 16,
            "spd_cfg": [1] * 15 + [6],
            "spd_act": [5, 5, 5, 0, 5, 4, 0, 0, 6, 6, 6, 0, 0, 0, 0, 6],
            "fc_cfg": [0] * 15 + [1],
            "fc_act": [0] * 15 + [1],
        }
        self.max_port_num = 16

    # -- transport / assertions ----------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.calls if r.method == method and r.url.path == path)

    def poe_write_bodies(self) -> list[dict[str, str]]:
        return [
            dict(parse_qsl(r.content.decode(), keep_blank_values=True))
            for r in self.calls
            if r.method == "POST" and r.url.path == "/poe_port_config.cgi"
        ]

    def evict(self) -> None:
        """Drop the device-side session (the owner logged into the web UI)."""
        self.device_authed = False

    # -- request handling ----------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        method, path = request.method, request.url.path
        if path == "/":
            return self._login_page()
        if path == "/logon.cgi" and method == "POST":
            self.logins += 1
            if self.login_errtype == 0:
                self.device_authed = True
            return httpx.Response(200, text=fixture(_ERRTYPE_FIXTURE[self.login_errtype]))
        if path == "/Logout.htm":
            self.device_authed = False
            return self._login_page()
        if path == "/SystemInfoRpm.htm":
            return self._guarded(lambda: httpx.Response(200, text=fixture("system_info.html")))
        if path == "/poe_port_config.cgi" and method == "POST":
            if self.reset_on_poe_write > 0:
                self.reset_on_poe_write -= 1
                raise httpx.ConnectError("connection reset")
            return self._guarded(lambda: self._write_poe(request))
        if path == "/port_setting.cgi":
            return self._guarded(lambda: self._write_port(request))
        if path == "/PoeConfigRpm.htm":
            return self._guarded(lambda: httpx.Response(200, text=self._render_poe()))
        if path == "/PortSettingRpm.htm":
            return self._guarded(lambda: httpx.Response(200, text=self._render_ports()))
        if path == "/PortStatisticsRpm.htm":
            return self._guarded(lambda: httpx.Response(200, text=fixture("port_statistics.html")))
        if path == "/Vlan8021QRpm.htm":
            return self._guarded(self._render_vlans)
        if path == "/Vlan8021QPvidRpm.htm":
            return self._guarded(lambda: httpx.Response(200, text=fixture("vlan_8021q_pvid.html")))
        return httpx.Response(404, text=f"no route for {path}")

    def _guarded(self, render: Callable[[], httpx.Response]) -> httpx.Response:
        if not self.device_authed:
            return self._login_page()
        return render()

    def _login_page(self) -> httpx.Response:
        return httpx.Response(200, text=fixture("login_page.html"))

    def _render_vlans(self) -> httpx.Response:
        if self.vlan_unexpected:
            return httpx.Response(200, text="<html><body>no vlan page here</body></html>")
        return httpx.Response(200, text=fixture("vlan_8021q.html"))

    def _write_poe(self, request: httpx.Request) -> httpx.Response:
        fields = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        port = next(int(k[4:]) for k in fields if k.startswith("sel_"))
        idx = port - 1
        turning_on = fields["name_pstate"] == "2"
        if turning_on and self.ignore_poe_on > 0:
            self.ignore_poe_on -= 1  # silently drop this write (port stays as-is)
            return httpx.Response(200, text=self._render_poe())
        self.poe["state"][idx] = 1 if turning_on else 0
        priority = int(fields["name_ppriority"]) - 1
        if self.clobber_poe_priority:
            priority = (priority + 1) % 3
        self.poe["priority"][idx] = priority
        self.poe["powerlimit"][idx] = _limit_from_code(
            int(fields["name_ppowerlimit"]), fields.get("name_ppowerlimit2", "")
        )
        if turning_on and self.power_restores:
            self.poe["power"][idx] = 40
            self.poe["current"][idx] = 76
            self.poe["voltage"][idx] = 520
            self.poe["powerstatus"][idx] = 2
            self.poe["pdclass"][idx] = 330
        else:
            self.poe["power"][idx] = 0
            self.poe["current"][idx] = 0
            self.poe["voltage"][idx] = 0
            self.poe["powerstatus"][idx] = 0
            if not turning_on:
                self.poe["pdclass"][idx] = 0
        return httpx.Response(200, text=self._render_poe())

    def _write_port(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        idx = int(params["portid"]) - 1
        self.port["state"][idx] = int(params["state"])
        speed = int(params["speed"])
        self.port["spd_cfg"][idx] = (speed % 6) + 1 if self.clobber_port_speed else speed
        self.port["fc_cfg"][idx] = int(params["flowcontrol"])
        return httpx.Response(200, text=self._render_ports())

    def _render_poe(self) -> str:
        p = self.poe
        g = self.poe_global
        return (
            "<!DOCTYPE html>\n<script>\n"
            f"var poe_port_num = {self.poe_port_num};\n"
            "var portConfig = {\n"
            f"state:[{_join(p['state'])}],\n"
            f"priority:[{_join(p['priority'])}],\n"
            f"powerlimit:[{_join(p['powerlimit'])}],\n"
            f"power:[{_join(p['power'])}],\n"
            f"current:[{_join(p['current'])}],\n"
            f"voltage:[{_join(p['voltage'])}],\n"
            f"pdclass:[{_join(p['pdclass'])}],\n"
            f"powerstatus:[{_join(p['powerstatus'])}]\n"
            "};\n"
            "var globalConfig = {\n"
            f"system_power_limit:{g[0]},\n"
            f"system_power_consumption:{g[1]},\n"
            f"system_power_remain:{g[2]},\n"
            f"system_power_limit_min:{g[3]},\n"
            f"system_power_limit_max:{g[4]}\n"
            '};\nvar tip = "";\n</script>\n<html><body></body></html>'
        )

    def _render_ports(self) -> str:
        pad = [0, 0]
        p = self.port
        return (
            "<!DOCTYPE html>\n<script>\n"
            f"var max_port_num = {self.max_port_num};\n"
            "var all_info = {\n"
            f"state:[{_join(p['state'] + pad)}],\n"
            f"trunk_info:[{_join(p['trunk_info'] + pad)}],\n"
            f"spd_cfg:[{_join(p['spd_cfg'] + pad)}],\n"
            f"spd_act:[{_join(p['spd_act'] + pad)}],\n"
            f"fc_cfg:[{_join(p['fc_cfg'] + pad)}],\n"
            f"fc_act:[{_join(p['fc_act'] + pad)}]\n"
            '};\nvar tip = "";\n</script>\n<html><body></body></html>'
        )


def _join(values: list[int]) -> str:
    return ",".join(str(v) for v in values)


def _limit_from_code(code: int, limit2: str) -> int:
    mapping = {1: 330, 2: 40, 3: 70, 4: 154, 5: 300}
    if code in mapping:
        return mapping[code]
    try:
        return round(float(limit2) * 10)
    except ValueError:
        return 330


class FakeClock:
    """A monotonic fake clock whose ``sleep`` advances time instantly."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def write_backend(
    tmp_path: Path, switch: StatefulSwitch, *, clock: FakeClock | None = None, **overrides: object
) -> tuple[EasySmartSwitchBackend, FakeClock]:
    """A backend wired to a stateful switch with writes enabled and a fake clock."""
    opts: dict[str, object] = {
        "allow_writes": "true",
        "protected_ports": "16",
        "port_map": "cam1=1;uplink=16",
    }
    opts.update(overrides)
    settings = make_settings(tmp_path, **opts)
    clock = clock or FakeClock()
    backend = EasySmartSwitchBackend(
        settings, transport=switch.transport(), now=clock.now, sleep=clock.sleep
    )
    return backend, clock


def read_backend(
    tmp_path: Path, switch: StatefulSwitch, **overrides: object
) -> EasySmartSwitchBackend:
    settings = make_settings(tmp_path, **overrides)
    return EasySmartSwitchBackend(settings, transport=switch.transport())


def build_mcp(backend: EasySmartSwitchBackend):
    """A FastMCP app wired to this backend (no sockets opened)."""
    from tplink_easysmart_mcp.core.config import load_global_settings
    from tplink_easysmart_mcp.server import MCP_ENV_PREFIX, build_server

    mcp, _ = build_server(
        load_global_settings(MCP_ENV_PREFIX, {}), backend.settings, backend=backend
    )
    return mcp


async def call(mcp, name: str, args: dict | None = None) -> dict:
    """Invoke a registered tool and return its envelope dict, however FastMCP wraps it."""
    import json as _json

    result = await mcp.call_tool(name, args or {})
    structured = result[1] if isinstance(result, tuple) else result
    if isinstance(structured, dict) and "result" in structured and "success" not in structured:
        structured = structured["result"]
    if isinstance(structured, dict) and "success" in structured:
        return structured
    content = result[0] if isinstance(result, tuple) else result
    return _json.loads(content[0].text)

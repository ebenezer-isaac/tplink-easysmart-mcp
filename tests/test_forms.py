"""Tests for the pure form builders (read-modify-write, redaction, guards)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tplink_easysmart_mcp.switch.constants import AUTO_LIMIT2
from tplink_easysmart_mcp.switch.errors import RestoredAccountMode
from tplink_easysmart_mcp.switch.forms import (
    build_login_form,
    build_poe_port_form,
    build_port_setting_query,
    plan_redacted,
)
from tplink_easysmart_mcp.switch.models import (
    LoginMode,
    PoeLimitKind,
    PoePort,
    PoePriority,
    PoeStatus,
)
from tplink_easysmart_mcp.switch.parsers import parse_poe, parse_ports

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _expected(manifest_fields: dict[str, str]) -> list[tuple[str, str]]:
    out = []
    for key, value in manifest_fields.items():
        if isinstance(value, str) and value.startswith("<AUTO_LIMIT2"):
            value = AUTO_LIMIT2
        out.append((key, value))
    return out


# ---- PoE read-modify-write ---------------------------------------------------


def test_poe_rmw_bodies_match_manifest_with_field_order() -> None:
    ports = {p.number: p for p in parse_poe(_fx("poe_config.html")).ports}
    examples = MANIFEST["fixtures"]["poe_config.html"]["expect"]["rmw_write_examples"]
    cases = {
        "port1_off": (1, False),
        "port3_on": (3, True),
        "port5_off": (5, False),
        "port8_on": (8, True),
    }
    for key, (num, enabled) in cases.items():
        plan = build_poe_port_form(ports[num], enabled)
        assert list(plan.fields) == _expected(examples[key]), key


@pytest.mark.parametrize(
    ("priority_raw", "expected_code"),
    [(0, "1"), (1, "2"), (2, "3")],
)
def test_priority_write_is_plus_one(priority_raw: int, expected_code: str) -> None:
    port = _poe(priority_raw=priority_raw)
    assert dict(build_poe_port_form(port, True).fields)["name_ppriority"] == expected_code


@pytest.mark.parametrize(
    ("kind", "powerlimit_raw", "limit_w", "code", "limit2"),
    [
        (PoeLimitKind.AUTO, 330, None, "1", AUTO_LIMIT2),
        (PoeLimitKind.CLASS1, 40, 4.0, "2", "(4w)"),
        (PoeLimitKind.CLASS2, 70, 7.0, "3", "(7w)"),
        (PoeLimitKind.CLASS3, 154, 15.4, "4", "(15.4w)"),
        (PoeLimitKind.CLASS4, 300, 30.0, "5", "(30w)"),
        (PoeLimitKind.MANUAL, 255, 25.5, "6", "25.5"),
    ],
)
def test_each_limit_code_and_limit2(kind, powerlimit_raw, limit_w, code, limit2) -> None:
    port = _poe(limit_kind=kind, limit_w=limit_w, powerlimit_raw=powerlimit_raw)
    fields = dict(build_poe_port_form(port, True).fields)
    assert fields["name_ppowerlimit"] == code
    assert fields["name_ppowerlimit2"] == limit2


def test_manual_equal_to_preset_is_rewritten_as_preset() -> None:
    # A manual 15.4 W reads back as the class-3 preset (154), so RMW re-sends the
    # preset code 4 and "(15.4w)" — documented and effectively identical.
    port = {p.number: p for p in parse_poe(_fx("poe_config.html")).ports}[3]
    fields = dict(build_poe_port_form(port, True).fields)
    assert port.limit_kind is PoeLimitKind.CLASS3
    assert fields["name_ppowerlimit"] == "4" and fields["name_ppowerlimit2"] == "(15.4w)"


def test_poe_selects_exactly_one_port() -> None:
    plan = build_poe_port_form(_poe(number=5), False)
    sel_fields = [k for k, _ in plan.fields if k.startswith("sel_")]
    assert sel_fields == ["sel_5"]


def test_request_plan_as_dict() -> None:
    plan = build_poe_port_form(_poe(number=2), True)
    assert plan.as_dict()["sel_2"] == "1" and plan.as_dict()["applay"] == "Apply"


@pytest.mark.parametrize("number", [0, 9, 17])
def test_poe_builder_rejects_out_of_range_ports(number: int) -> None:
    with pytest.raises(ValidationError):
        build_poe_port_form(_poe(number=number), True)


# ---- port setting ------------------------------------------------------------


def test_port_setting_query_matches_manifest() -> None:
    port5 = {p.number: p for p in parse_ports(_fx("port_setting.html"))}[5]
    plan = build_port_setting_query(port5, enabled=False)
    url = f"{plan.method} {plan.path}?" + "&".join(f"{k}={v}" for k, v in plan.fields)
    assert (
        url == MANIFEST["fixtures"]["port_setting.html"]["expect"]["write_example_disable_port_5"]
    )


def test_port_setting_resends_current_speed_and_fc() -> None:
    port16 = {p.number: p for p in parse_ports(_fx("port_setting.html"))}[16]
    fields = dict(build_port_setting_query(port16, enabled=True).fields)
    assert fields["speed"] == "6" and fields["flowcontrol"] == "1"  # port16 spd_cfg=6, fc_cfg=1


# ---- login form guard --------------------------------------------------------


def test_login_form_normal() -> None:
    plan = build_login_form("admin", "pw123456", LoginMode.NORMAL)
    assert plan.method == "POST" and plan.path == "/logon.cgi"
    assert dict(plan.fields) == {
        "username": "admin",
        "password": "pw123456",
        "cpassword": "",
        "logon": "Login",
    }


def test_cpassword_is_always_empty() -> None:
    assert dict(build_login_form("admin", "pw123456", LoginMode.NORMAL).fields)["cpassword"] == ""


def test_login_form_refuses_restored_account() -> None:
    with pytest.raises(RestoredAccountMode):
        build_login_form("admin", "pw123456", LoginMode.RESTORED_ACCOUNT)


def test_password_never_in_redacted_plan() -> None:
    plan = build_login_form("admin", "SuperSecret9", LoginMode.NORMAL)
    redacted = plan_redacted(plan)
    assert dict(redacted.fields)["password"] == "<redacted>"
    assert "SuperSecret9" not in str(redacted.fields)
    assert dict(plan.fields)["password"] == "SuperSecret9"  # original untouched


# ---- helper ------------------------------------------------------------------


def _poe(
    *,
    number: int = 1,
    priority_raw: int = 0,
    limit_kind: PoeLimitKind = PoeLimitKind.AUTO,
    limit_w: float | None = None,
    powerlimit_raw: int = 330,
) -> PoePort:
    return PoePort(
        number=number,
        enabled=True,
        priority=PoePriority.from_code(priority_raw),
        limit_kind=limit_kind,
        limit_w=limit_w,
        power_w=0.0,
        current_ma=0,
        voltage_v=0.0,
        pd_class=None,
        status=PoeStatus.OFF,
        raw=(1, priority_raw, powerlimit_raw),
    )

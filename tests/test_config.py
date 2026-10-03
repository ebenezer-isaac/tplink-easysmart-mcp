"""Tests for SwitchSettings and its loader (prefix EASYSMART_)."""

from __future__ import annotations

import pytest

from tplink_easysmart_mcp.core.errors import ConfigError
from tplink_easysmart_mcp.switch.config import (
    SwitchSettings,
    load_switch_settings,
    poe_ports_mismatch,
)

PREFIX = "EASYSMART_"
HOST = "192.0.2.10"
PASSWORD = "TestPass123"


def env(**overrides: str) -> dict[str, str]:
    base = {f"{PREFIX}HOST": HOST, f"{PREFIX}PASSWORD": PASSWORD}
    base.update({f"{PREFIX}{k}": v for k, v in overrides.items()})
    return base


def load(**overrides: str) -> SwitchSettings:
    return load_switch_settings(env(**overrides))


# ---- defaults / transport ----------------------------------------------------


def test_defaults() -> None:
    s = load()
    assert s.port == 80
    assert s.base_url == "http://192.0.2.10:80"
    assert s.referer == "http://192.0.2.10/"
    assert s.timeout_seconds == 5.0
    assert s.poe_ports == tuple(range(1, 9))
    assert s.protected_ports == ()
    assert s.login_cooldown_s == 300
    assert s.allow_writes is False


def test_password_never_in_repr() -> None:
    s = load()
    assert PASSWORD not in repr(s)


# ---- password ----------------------------------------------------------------


@pytest.mark.parametrize("password", ["short", "x" * 17, "with space12", "ab cd1"])
def test_bad_passwords_rejected(password: str) -> None:
    with pytest.raises(ConfigError):
        load(**{"PASSWORD": password})


@pytest.mark.parametrize("password", ["sixchr", "x" * 16, "TestPass123"])
def test_valid_passwords_accepted(password: str) -> None:
    assert load(**{"PASSWORD": password}).password.get_secret_value() == password


# ---- write guards ------------------------------------------------------------


def test_allow_writes_requires_protected_ports() -> None:
    with pytest.raises(ConfigError, match="PROTECTED_PORTS"):
        load(ALLOW_WRITES="true")


def test_allow_writes_with_protected_ports_ok() -> None:
    s = load(ALLOW_WRITES="true", PROTECTED_PORTS="15,16")
    assert s.allow_writes is True
    assert s.protected_ports == (15, 16)


def test_allow_writes_rejects_empty_poe_ports() -> None:
    with pytest.raises(ConfigError, match="POE_PORTS"):
        load(ALLOW_WRITES="true", PROTECTED_PORTS="16", POE_PORTS="")


def test_empty_poe_ports_allowed_without_writes() -> None:
    assert load(POE_PORTS="").poe_ports == ()


# ---- TLS rejection -----------------------------------------------------------


@pytest.mark.parametrize("key", ["VERIFY_TLS", "TLS_FINGERPRINT_SHA256"])
def test_tls_settings_are_rejected(key: str) -> None:
    with pytest.raises(ConfigError):
        load_switch_settings({**env(), f"{PREFIX}{key}": "true"})


# ---- unknown keys ------------------------------------------------------------


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ConfigError, match="PROTECTED_PORT"):
        load_switch_settings({**env(), f"{PREFIX}PROTECTED_PORT": "16"})


def test_mcp_subprefix_keys_are_ignored() -> None:
    load_switch_settings({**env(), f"{PREFIX}MCP_PORT": "8765"})  # does not raise


# ---- POE_PORTS parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1-8", tuple(range(1, 9))), ("1,2,5-8", (1, 2, 5, 6, 7, 8)), ("16", (16,))],
)
def test_poe_ports_valid(spec: str, expected: tuple[int, ...]) -> None:
    assert load(POE_PORTS=spec).poe_ports == expected


@pytest.mark.parametrize("spec", ["8-5", "0", "junk", "1,,2", "70", "-1"])
def test_poe_ports_invalid(spec: str) -> None:
    with pytest.raises(ConfigError):
        load(POE_PORTS=spec)


# ---- PORT_MAP parsing --------------------------------------------------------


def test_port_map_valid() -> None:
    s = load(PORT_MAP="cam1=1;Uplink=16;server=15")
    assert s.port_map_dict == {"cam1": 1, "uplink": 16, "server": 15}
    assert s.summary()["port_map_names"] == ["cam1", "server", "uplink"]


@pytest.mark.parametrize(
    "spec",
    [
        "cam1=1;CAM1=2",  # duplicate name (case-insensitive)
        "cam/bad=1",  # bad characters
        "cam=0",  # port 0
        "cam=1;",  # trailing ';'
        ";cam=1",  # leading ';'
        "café=1",  # unicode name
        "cam=1;=2",  # empty name entry
        "cam",  # missing '=port'
        "cam=99",  # out of range
    ],
)
def test_port_map_invalid(spec: str) -> None:
    with pytest.raises(ConfigError):
        load(PORT_MAP=spec)


def test_port_map_duplicate_port_allowed_with_warning(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        s = load(PORT_MAP="a=1;b=1")
    assert s.port_map_dict == {"a": 1, "b": 1}
    assert any("mapped by both" in r.message for r in caplog.records)


# ---- cross-field / mismatch --------------------------------------------------


def test_cycle_bounds_rejected_when_reversed() -> None:
    with pytest.raises(ConfigError):
        load(CYCLE_OFF_MIN_S="120", CYCLE_OFF_MAX_S="5")


def test_poe_ports_mismatch_detects_extra_and_missing() -> None:
    s = load(POE_PORTS="1-8")
    assert poe_ports_mismatch(s, 8) is None
    warning = poe_ports_mismatch(s, 4)
    assert warning is not None and "exceed" in warning


def test_direct_construction_cannot_bypass_write_guard() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SwitchSettings.model_validate(
            {"env_prefix": PREFIX, "host": HOST, "password": PASSWORD, "allow_writes": True}
        )

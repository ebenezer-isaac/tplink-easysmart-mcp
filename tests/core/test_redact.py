from __future__ import annotations

import copy

import pytest

from tplink_easysmart_mcp.core.redact import REDACTED, TRUNCATED, redact


def test_redacts_sensitive_top_level_keys() -> None:
    src = {
        "password": "p",
        "cpassword": "c",
        "cookie": "x",
        "H_P_SSID": "tplink_abc",
        "token": "t",
        "secret": "s",
        "Authorization": "Basic zzz",
        "name": "cam",
    }
    assert redact(src) == {
        "password": REDACTED,
        "cpassword": REDACTED,
        "cookie": REDACTED,
        "H_P_SSID": REDACTED,
        "token": REDACTED,
        "secret": REDACTED,
        "Authorization": REDACTED,
        "name": "cam",
    }


@pytest.mark.parametrize(
    "field",
    [
        "Password",
        "PASSWORD",
        "new_password",
        "passwordConfirm",
        "passwd",
        "user_pwd",
        "api_token",
        "SessionToken",
        "client_secret",
        "Set-Cookie",
        "h_p_ssid",
    ],
)
def test_sensitive_substrings_are_redacted_case_insensitively(field: str) -> None:
    assert redact({field: "v"}) == {field: REDACTED}


# Negative set: the switch's port / PoE / system fields must never be eaten.
@pytest.mark.parametrize(
    "field",
    [
        "power_w",
        "voltage_v",
        "current_ma",
        "pd_class",
        "priority",
        "portid",
        "poe_port_num",
        "name_ppowerlimit",
        "name_pstate",
        "speed_config",
        "link_up",
        "username",
        "firmware",
        "hardware",
        "session_model",
        "max_port_num",
    ],
)
def test_port_and_poe_field_names_survive(field: str) -> None:
    assert redact({field: "v"}) == {field: "v"}


def test_nested_dicts_and_lists() -> None:
    src = {
        "ports": [
            {"port_1": {"ip": "192.0.2.21", "power_w": 4.1, "auth": {"password": "x"}}},
            {"port_2": {"ip": "192.0.2.22", "user": "admin"}},
        ],
        "meta": [[{"cookie": "t"}]],
    }
    assert redact(src) == {
        "ports": [
            {"port_1": {"ip": "192.0.2.21", "power_w": 4.1, "auth": {"password": REDACTED}}},
            {"port_2": {"ip": "192.0.2.22", "user": "admin"}},
        ],
        "meta": [[{"cookie": REDACTED}]],
    }


def test_sensitive_container_values_are_replaced_wholesale() -> None:
    assert redact({"token": {"n": 1, "e": 2}}) == {"token": REDACTED}
    assert redact({"password": ["a", "b"]}) == {"password": REDACTED}


def test_input_is_never_mutated() -> None:
    src = {"a": [{"password": "x", "b": {"cookie": "y"}}], "t": ("k", {"token": 1})}
    snapshot = copy.deepcopy(src)
    out = redact(src)
    assert src == snapshot
    assert out is not src
    assert out["a"] is not src["a"]
    assert out["a"][0] is not src["a"][0]


def test_tuples_become_lists() -> None:
    assert redact(("a", {"token": 1})) == ["a", {"token": REDACTED}]


@pytest.mark.parametrize("value", [None, 0, 1.5, True, "text", ""])
def test_scalars_pass_through(value: object) -> None:
    assert redact(value) == value


def test_non_string_keys_are_preserved() -> None:
    assert redact({1: "a", 2: {"cookie": "x"}}) == {1: "a", 2: {"cookie": REDACTED}}


def test_depth_bomb_is_truncated_not_crashing() -> None:
    deep: dict = {}
    cursor = deep
    for _ in range(500):
        cursor["n"] = {}
        cursor = cursor["n"]
    out = redact(deep)
    depth = 0
    node = out
    while isinstance(node, dict) and "n" in node:
        node = node["n"]
        depth += 1
    assert node == TRUNCATED
    assert depth <= 64


def test_cyclic_structure_does_not_recurse_forever() -> None:
    a: dict = {"x": 1}
    a["self"] = a
    out = redact(a)
    assert out["x"] == 1

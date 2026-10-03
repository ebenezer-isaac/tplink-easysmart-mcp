from __future__ import annotations

import pytest

from tests.core.helpers import PREFIX, env
from tplink_easysmart_mcp.core import breaker, envelope
from tplink_easysmart_mcp.core.config import load_device_settings
from tplink_easysmart_mcp.core.errors import (
    ApiError,
    AuthFailed,
    BreakerOpen,
    InvalidInput,
    LockoutGuard,
    PreconditionFailed,
    TokenExpired,
)
from tplink_easysmart_mcp.core.state import Outcome
from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.core.write_gate import check_write_gate


def test_ok_shape() -> None:
    assert envelope.ok({"a": 1}) == {"success": True, "data": {"a": 1}, "error": None}
    assert envelope.ok([]) == {"success": True, "data": [], "error": None}


def test_fail_shape_and_details_copied() -> None:
    details = {"k": 1}
    result = envelope.fail("X", "m", details)
    details["k"] = 2
    assert result == {
        "success": False,
        "data": None,
        "error": {"code": "X", "message": "m", "details": {"k": 1}},
    }
    assert envelope.fail("X", "m")["error"]["details"] == {}


def test_auth_failed_surfaces_attempts_left_and_max() -> None:
    result = envelope.from_error(
        AuthFailed("bad", code=-1, symbol="EUNAUTH", attempts_left=3, max_attempts=10)
    )
    assert result["error"]["code"] == "AUTH_FAILED"
    assert result["error"]["details"] == {
        "device_error_code": -1,
        "symbol": "EUNAUTH",
        "attempts_left": 3,
        "max_attempts": 10,
        "lock_seconds_left": None,
        "locked": False,
    }


@pytest.mark.parametrize(
    ("kwargs", "locked"),
    [
        ({"locked": True}, True),
        ({"attempts_left": 0}, True),
        ({"lock_seconds_left": 60}, True),
        ({"attempts_left": 1}, False),
        ({}, False),
    ],
)
def test_auth_failed_locked_flag(kwargs: dict, locked: bool) -> None:
    assert AuthFailed("x", **kwargs).locked is locked


def test_api_error_includes_symbol_and_meaning() -> None:
    err = ApiError(-40209, symbol="EINVARG", meaning="Invalid argument")
    assert "EINVARG" in str(err)
    details = envelope.from_error(err)["error"]["details"]
    assert details == {
        "device_error_code": -40209,
        "symbol": "EINVARG",
        "meaning": "Invalid argument",
    }


def test_precondition_failed_details() -> None:
    err = PreconditionFailed("m", reason="MISMATCH", context={"port": "1"})
    assert envelope.from_error(err)["error"]["details"] == {"reason": "MISMATCH", "port": "1"}


def test_error_kinds_are_distinct() -> None:
    kinds = {AuthFailed.kind, TokenExpired.kind, LockoutGuard.kind, ApiError.kind}
    assert len(kinds) == 4


# ---- write gate ---------------------------------------------------------------


def _settings(**overrides: str):
    return load_device_settings(PREFIX, env(**overrides))


def test_write_gate_read_passes() -> None:
    assert check_write_gate(_settings(), "get", False) is None


@pytest.mark.parametrize("confirm", [False, True, "true", 1])
def test_write_gate_refuses_when_disallowed(confirm: object) -> None:
    refusal = check_write_gate(_settings(), "set", confirm)
    assert refusal is not None
    assert "EASYSMART_ALLOW_WRITES" in refusal["error"]["message"]


@pytest.mark.parametrize("confirm", [False, "true", "yes", 1, None])
def test_write_gate_requires_literal_true(confirm: object) -> None:
    refusal = check_write_gate(_settings(ALLOW_WRITES="true"), "do", confirm)
    assert refusal is not None and "confirm_write" in refusal["error"]["message"]


def test_write_gate_passes_with_both_keys() -> None:
    assert check_write_gate(_settings(ALLOW_WRITES="true"), "delete", True) is None


# ---- file-backed login breaker --------------------------------------------------


def test_breaker_starts_closed_and_reserve_passes(tmp_path) -> None:
    b = breaker.LoginBreaker(tmp_path, "192.0.2.10", max_failures=2)
    assert not b.path.exists()
    assert b.status()["state"] == "closed"
    b.reserve_attempt().release(Outcome.SUCCESS)  # does not raise
    assert b.status()["state"] == "closed"


def test_breaker_trips_and_blocks_then_clears(tmp_path) -> None:
    b = breaker.LoginBreaker(tmp_path, "192.0.2.10")
    b.reserve_attempt().release(Outcome.FAILURE, failure={"code": "AUTH_FAILED", "err_type": 1})
    assert b.status()["state"] == "open"
    with pytest.raises(BreakerOpen):
        b.reserve_attempt()
    b.clear()
    assert b.status()["state"] == "closed"
    b.reserve_attempt().release(Outcome.SUCCESS)  # admits again


def test_breaker_persists_across_instances(tmp_path) -> None:
    breaker.LoginBreaker(tmp_path, "sw").reserve_attempt().release(
        Outcome.FAILURE, failure={"err_type": 2}
    )
    reopened = breaker.LoginBreaker(tmp_path, "sw")
    assert reopened.status()["state"] == "open"
    assert reopened.status()["last_failure"]["err_type"] == 2
    with pytest.raises(BreakerOpen):
        reopened.reserve_attempt()


def test_breaker_file_is_private(tmp_path) -> None:
    import os
    import sys

    b = breaker.LoginBreaker(tmp_path, "sw")
    b.reserve_attempt().release(Outcome.FAILURE)
    if sys.platform != "win32":
        assert (os.stat(b.path).st_mode & 0o777) == 0o600


def test_breaker_fails_closed_on_corrupt_file(tmp_path) -> None:
    b = breaker.LoginBreaker(tmp_path, "sw")
    b.path.parent.mkdir(parents=True, exist_ok=True)
    b.path.write_text("{ this is not json", encoding="utf-8")
    assert b.status()["state"] == "open"
    with pytest.raises(BreakerOpen):
        b.reserve_attempt()


def test_breaker_fails_closed_on_malformed_json(tmp_path) -> None:
    b = breaker.LoginBreaker(tmp_path, "sw")
    b.path.parent.mkdir(parents=True, exist_ok=True)
    b.path.write_text('{"open": "yes"}', encoding="utf-8")  # unknown shape; extra forbidden
    assert b.status()["state"] == "open"
    with pytest.raises(BreakerOpen):
        b.reserve_attempt()


def test_breaker_status_is_json_safe(tmp_path) -> None:
    import json

    b = breaker.LoginBreaker(tmp_path, "sw")
    b.reserve_attempt().release(Outcome.FAILURE, failure={"err_type": 1})
    snap = b.status()
    json.dumps(snap)  # does not raise
    assert snap["state"] == "open"
    assert snap["last_failure"] == {"err_type": 1}


def test_login_disabled_is_owned_by_the_breaker() -> None:
    # LOGIN_DISABLED remains a settings flag; it is now passed into the breaker, which
    # refuses at reserve_attempt() before any store I/O.
    assert _settings(LOGIN_DISABLED="true").login_disabled is True


# ---- run_tool -----------------------------------------------------------------


async def test_run_tool_maps_errors_and_redacts() -> None:
    async def leaky():
        return {"token": "s", "ok": 1}

    async def bad_input():
        raise InvalidInput("nope")

    async def boom():
        raise RuntimeError("internal detail")

    assert (await run_tool("t", leaky))["data"] == {"token": "<redacted>", "ok": 1}
    assert (await run_tool("t", bad_input))["error"]["code"] == "INVALID_INPUT"
    internal = await run_tool("t", boom)
    assert internal["error"]["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in internal["error"]["message"]


# ---- serial lock / transport masks / cli helpers ----------------------------------


async def test_serial_lock_serialises() -> None:
    import asyncio

    from tplink_easysmart_mcp.core.serial import SerialLock

    lock, order = SerialLock(), []

    async def job(name: str) -> str:
        order.append(f"{name}-start")
        await asyncio.sleep(0)
        order.append(f"{name}-end")
        return name

    results = await asyncio.gather(lock.run(lambda: job("a")), lock.run(lambda: job("b")))
    assert results == ["a", "b"]
    assert order == ["a-start", "a-end", "b-start", "b-end"]
    assert lock.busy is False


def test_cli_helpers(capsys) -> None:
    from tplink_easysmart_mcp.core.cli import configure_logging, emit_envelope, emit_lines

    configure_logging("NOPE_LEVEL", {"NOPE_LEVEL": "bogus"})
    assert emit_envelope(envelope.ok({"a": 1})) == 0
    assert emit_envelope(envelope.fail("X", "m")) == 1
    assert emit_lines(["one", "two"]) == 0
    out = capsys.readouterr().out
    assert '"success": true' in out and out.rstrip().endswith("two")

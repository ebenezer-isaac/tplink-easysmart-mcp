"""Device-agnostic coverage for core.transport (httpx JSON transport + log masking)."""

from __future__ import annotations

import httpx
import pytest

from tplink_easysmart_mcp.core import transport as T
from tplink_easysmart_mcp.core.errors import TransportError


def _transport(handler) -> T.JsonHttpTransport:
    return T.JsonHttpTransport(
        "https://192.0.2.10:443",
        verify_tls=False,
        timeout_seconds=5.0,
        headers={"X-Test": "1"},
        http_transport=httpx.MockTransport(handler),
    )


async def test_post_json_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-test"] == "1"
        return httpx.Response(200, json={"error_code": 0, "ok": True})

    t = _transport(handler)
    try:
        assert await t.post_json("/", {"method": "get"}) == {"error_code": 0, "ok": True}
    finally:
        await t.aclose()


async def test_post_json_non_200_raises() -> None:
    t = _transport(lambda r: httpx.Response(503))
    try:
        with pytest.raises(TransportError, match="HTTP 503"):
            await t.post_json("/", {})
    finally:
        await t.aclose()


async def test_post_json_non_json_raises() -> None:
    t = _transport(lambda r: httpx.Response(200, content=b"not json"))
    try:
        with pytest.raises(TransportError, match="non-JSON"):
            await t.post_json("/", {})
    finally:
        await t.aclose()


async def test_post_json_non_object_raises() -> None:
    t = _transport(lambda r: httpx.Response(200, json=[1, 2, 3]))
    try:
        with pytest.raises(TransportError, match="not an object"):
            await t.post_json("/", {})
    finally:
        await t.aclose()


async def test_post_json_timeout_and_httperror() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    t1, t2 = _transport(timeout), _transport(refused)
    try:
        with pytest.raises(TransportError, match="timed out"):
            await t1.post_json("/", {})
        with pytest.raises(TransportError, match="failed"):
            await t2.post_json("/", {})
    finally:
        await t1.aclose()
        await t2.aclose()


async def test_post_json_too_large(monkeypatch) -> None:
    monkeypatch.setattr(T, "MAX_RESPONSE_BYTES", 4)
    t = _transport(lambda r: httpx.Response(200, json={"a": 123456789}))
    try:
        with pytest.raises(TransportError, match="size limit"):
            await t.post_json("/", {})
    finally:
        await t.aclose()


async def test_get_bytes_success_and_cap() -> None:
    t = _transport(lambda r: httpx.Response(200, content=b"abcdefgh"))
    try:
        assert await t.get_bytes("/f", max_bytes=100) == b"abcdefgh"
        with pytest.raises(TransportError, match="size limit"):
            await t.get_bytes("/f", max_bytes=4)
    finally:
        await t.aclose()


async def test_get_bytes_non_200_and_errors() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    t1 = _transport(lambda r: httpx.Response(404))
    t2 = _transport(refused)
    try:
        with pytest.raises(TransportError, match="HTTP 404"):
            await t1.get_bytes("/f", max_bytes=10)
        with pytest.raises(TransportError, match="failed"):
            await t2.get_bytes("/f", max_bytes=10)
    finally:
        await t1.aclose()
        await t2.aclose()


def test_body_summary() -> None:
    assert T.body_summary({"method": "get", "system": {}, "network": {}}) == (
        "method=get modules=['network', 'system']"
    )


def test_token_masks_registered_once_and_applied() -> None:
    T.register_token_mask(r"sid=\w+", "sid=<x>")
    T.register_token_mask(r"sid=\w+", "sid=<x>")
    assert sum(p.pattern == r"sid=\w+" for p, _ in T._MASKS) == 1
    assert T.mask_tokens("/a?sid=abc123") == "/a?sid=<x>"

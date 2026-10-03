"""Coverage for canonical-core modules the switch does not itself exercise.

The X1b core sync brings the canonical ``core/`` verbatim, which is a superset of
what the HTTP-only switch uses: the JSON+TLS-pinning ``transport``, the RTSP/export
fields and validators of ``config.DeviceSettings``, and the ``serial.GuardedWriter``.
These behaviours are still part of the one shared core (hashed by the identity
test), so they are tested here directly, device-agnostically.
"""

from __future__ import annotations

from pathlib import Path

import httpcore
import httpx
import pytest

from tplink_easysmart_mcp.core import transport as T
from tplink_easysmart_mcp.core.config import DeviceSettings, normalise_fingerprint, validate_host
from tplink_easysmart_mcp.core.errors import TlsPinMismatch, TransportError, WriteNotEnabled
from tplink_easysmart_mcp.core.serial import GuardedWriter, SerialLock

DER = b"a-fake-der-certificate"
PIN = T.fingerprint_sha256(DER)


def _settings(**overrides: object) -> DeviceSettings:
    base: dict[str, object] = {
        "env_prefix": "EASYSMART_",
        "host": "192.0.2.5",
        "password": "secret",
    }
    base.update(overrides)
    return DeviceSettings.model_validate(base)


# ---- config: fingerprint / host / validators / properties -------------------


def test_normalise_fingerprint_accepts_colons_and_rejects_bad() -> None:
    assert normalise_fingerprint("AB:" * 31 + "AB") == "ab" * 32
    with pytest.raises(ValueError, match="hex string"):
        normalise_fingerprint(123)
    with pytest.raises(ValueError, match="SHA-256"):
        normalise_fingerprint("not-hex")


def test_validate_host_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_host(123)


def test_device_settings_media_fields_and_properties() -> None:
    s = _settings(
        host="2001:db8::1",
        tls_fingerprint_sha256="AB:" * 31 + "AB",
        rtsp_username="viewer",
        rtsp_password="rpw",
        export_dir="exports",
        ffmpeg_path="/usr/bin/ffmpeg",
        state_dir="/var/lib/x",
        backup_dir="backups",
    )
    assert s.tls_fingerprint_sha256 == "ab" * 32
    assert s.host_for_url == "[2001:db8::1]"
    assert s.effective_rtsp_username == "viewer"
    assert s.effective_rtsp_password == "rpw"
    assert s.export_path == Path("exports")


def test_rtsp_defaults_to_login_credentials() -> None:
    s = _settings(username="admin", password="loginpw")
    assert s.effective_rtsp_username == "admin"
    assert s.effective_rtsp_password == "loginpw"


@pytest.mark.parametrize(
    "overrides",
    [
        {"username": "has space"},
        {"backup_dir": "bad\x00dir"},
        {"export_dir": "bad\x01dir"},
        {"ffmpeg_path": "ff\x00mpeg"},
        {"rtsp_username": "bad name"},
        {"rtsp_password": "pw\x00bad"},
        {"state_dir": "   "},
        {"state_dir": "ctl\x00"},
    ],
)
def test_device_settings_validators_reject_bad_values(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _settings(**overrides)


# ---- serial.GuardedWriter ---------------------------------------------------


async def test_guarded_writer_dry_run_returns_request_without_io() -> None:
    gw = GuardedWriter(_settings(dry_run=True))
    assert gw.busy is False

    async def action() -> str:
        raise AssertionError("dry-run must not run the action")

    assert await gw.run({"module": "x"}, action) == {"dry_run": True, "request": {"module": "x"}}


async def test_guarded_writer_refuses_without_writes_enabled() -> None:
    gw = GuardedWriter(_settings(allow_writes=False))

    async def action() -> str:
        return "ran"

    with pytest.raises(WriteNotEnabled):
        await gw.run({}, action)


async def test_guarded_writer_runs_under_lock_when_enabled() -> None:
    gw = GuardedWriter(_settings(allow_writes=True), SerialLock())

    async def action() -> str:
        return "ran"

    assert await gw.run({}, action) == "ran"
    # require_gate=False runs even with writes off (a serialised read-only export).
    assert await GuardedWriter(_settings(allow_writes=False)).run({}, action, require_gate=False)


# ---- transport: masks, body summary, fingerprint ----------------------------


def test_register_token_mask_is_idempotent_and_masks_logs(caplog) -> None:
    T.register_token_mask(r"stok=[0-9a-f]+", "stok=<masked>")
    T.register_token_mask(r"stok=[0-9a-f]+", "stok=<masked>")  # idempotent
    masked = T.mask_tokens("GET /?stok=deadbeef")  # secret-scan: allow (public test vector)
    assert masked == "GET /?stok=<masked>"
    import logging

    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("x stok=cafef00d y")  # secret-scan: allow (test vector)
    assert "cafef00d" not in caplog.text


def test_body_summary_lists_modules_without_values() -> None:
    assert (
        T.body_summary({"method": "get", "poe": {}, "ports": {}})
        == "method=get modules=['poe', 'ports']"
    )


def test_verify_cert_fingerprint_match_and_mismatch() -> None:
    T.verify_cert_fingerprint(DER, PIN)  # no raise
    with pytest.raises(TlsPinMismatch):
        T.verify_cert_fingerprint(DER, "00" * 32)


# ---- transport: post_json / get_bytes over a mock transport -----------------


def _mk(handler, **kwargs) -> T.JsonHttpTransport:
    return T.JsonHttpTransport(
        "https://device.invalid",
        verify_tls=False,
        timeout_seconds=5.0,
        http_transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_post_json_happy_and_error_shapes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": 1})

    t = _mk(handler)
    assert await t.post_json("/api", {"method": "get"}) == {"ok": 1}
    await t.aclose()

    async def _expect(resp: httpx.Response, match: str) -> None:
        t = _mk(lambda _req: resp)
        with pytest.raises(TransportError, match=match):
            await t.post_json("/api", {"method": "get"})
        await t.aclose()

    await _expect(httpx.Response(500), "HTTP 500")
    await _expect(httpx.Response(200, text="not json"), "non-JSON")
    await _expect(httpx.Response(200, json=[1, 2]), "not an object")


async def test_post_json_oversize_and_timeout() -> None:
    big = _mk(lambda _req: httpx.Response(200, content=b"x" * (T.MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(TransportError, match="size limit"):
        await big.post_json("/api", {"method": "get"})
    await big.aclose()

    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    t = _mk(boom)
    with pytest.raises(TransportError, match="timed out"):
        await t.post_json("/api", {"method": "get"})
    await t.aclose()


async def test_get_bytes_streams_with_cap_and_reports_errors() -> None:
    t = _mk(lambda _req: httpx.Response(200, content=b"hello"))
    assert await t.get_bytes("/f", max_bytes=10) == b"hello"
    await t.aclose()

    t = _mk(lambda _req: httpx.Response(200, content=b"toolong"))
    with pytest.raises(TransportError, match="size limit"):
        await t.get_bytes("/f", max_bytes=3)
    await t.aclose()

    t = _mk(lambda _req: httpx.Response(404))
    with pytest.raises(TransportError, match="HTTP 404"):
        await t.get_bytes("/f", max_bytes=10)
    await t.aclose()

    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("dropped")

    t = _mk(boom)
    with pytest.raises(TransportError, match="failed"):
        await t.get_bytes("/f", max_bytes=10)
    await t.aclose()


# ---- transport: TLS pinning (fail-closed when the cert cannot be read) -------


async def test_pin_set_but_no_network_stream_fails_closed() -> None:
    # MockTransport exposes no network_stream, so the pin cannot be proven -> refuse.
    t = _mk(lambda _req: httpx.Response(200, json={"ok": 1}), tls_fingerprint=PIN)
    with pytest.raises(TransportError, match="certificate"):
        await t.post_json("/api", {"method": "get"})
    await t.aclose()


def test_verify_ssl_object_records_and_rejects() -> None:
    t = T.JsonHttpTransport("https://d", verify_tls=False, timeout_seconds=5.0, tls_fingerprint=PIN)

    class _SSL:
        def getpeercert(self, binary_form: bool) -> bytes:
            return DER

    t._verify_ssl_object(_SSL())  # matches the pin
    assert t.observed_fingerprint == PIN
    with pytest.raises(TlsPinMismatch, match="could not read"):
        t._verify_ssl_object(None)


# ---- transport: the pinning network backend / stream wrappers ---------------


class _FakeSSL:
    def getpeercert(self, binary_form: bool) -> bytes:
        return DER


class _FakeStream(httpcore.AsyncNetworkStream):
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # type: ignore[override]
        return self

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b"data"

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def get_extra_info(self, info: str):
        return _FakeSSL() if info == "ssl_object" else None


class _FakeBackend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # type: ignore[override]
        return _FakeStream()

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # type: ignore[override]
        return _FakeStream()

    async def sleep(self, seconds: float) -> None:
        return None


async def test_pinning_backend_verifies_leaf_cert_at_handshake() -> None:
    def verify(ssl_object) -> None:
        T.verify_cert_fingerprint(T._der_from_ssl_object(ssl_object), PIN)

    backend = T._PinningBackend(_FakeBackend(), verify)
    stream = await backend.connect_tcp("device.invalid", 443)
    tls = await stream.start_tls(None, server_hostname="device.invalid")  # verify runs here
    assert await tls.read(4) == b"data"
    await tls.write(b"x")
    assert tls.get_extra_info("ssl_object") is not None
    await tls.aclose()


def test_pinning_transport_is_built_when_a_pin_is_configured() -> None:
    t = T.JsonHttpTransport("https://d", verify_tls=False, timeout_seconds=5.0, tls_fingerprint=PIN)
    assert t.observed_fingerprint is None  # nothing handshaked yet

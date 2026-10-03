from __future__ import annotations

import pytest

from tests.core.helpers import DOC_HOST, MCP_PREFIX, PREFIX, TEST_PASSWORD, env
from tplink_easysmart_mcp.core.config import (
    DeviceSettings,
    host_is_set,
    load_device_settings,
    load_global_settings,
)
from tplink_easysmart_mcp.core.errors import ConfigError


def load(**overrides: str) -> DeviceSettings:
    return load_device_settings(PREFIX, env(**overrides))


def test_device_defaults() -> None:
    s = load()
    assert s.env_prefix == PREFIX
    assert s.host == DOC_HOST
    assert s.port == 443
    assert s.username == "admin"
    assert s.password.get_secret_value() == TEST_PASSWORD
    assert s.verify_tls is False
    assert s.allow_writes is False
    assert s.login_disabled is False
    assert s.dry_run is False
    assert s.max_login_failures == 1
    assert s.env_name("ALLOW_WRITES") == "EASYSMART_ALLOW_WRITES"


def test_prefixes_are_isolated() -> None:
    environ = {
        **env(),
        "OTHER_DEV_HOST": "192.0.2.1",
        "OTHER_DEV_PASSWORD": "other",
        "OTHER_DEV_PORT": "8443",
    }
    a = load_device_settings(PREFIX, environ)
    b = load_device_settings("OTHER_DEV_", environ)
    assert (a.host, a.port) == (DOC_HOST, 443)
    assert (b.host, b.port) == ("192.0.2.1", 8443)


def test_host_is_set() -> None:
    assert host_is_set(PREFIX, env()) is True
    assert host_is_set(PREFIX, {}) is False
    assert host_is_set(PREFIX, {f"{PREFIX}HOST": "   "}) is False


@pytest.mark.parametrize("prefix", ["bad_", "NOUNDERSCORE", "_X_", "A__"])
def test_bad_prefix_rejected(prefix: str) -> None:
    with pytest.raises(ConfigError):
        load_device_settings(prefix, {f"{prefix}HOST": DOC_HOST, f"{prefix}PASSWORD": "x"})
    with pytest.raises(ConfigError):
        load_global_settings(prefix, {})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("YES", True), ("false", False), ("0", False), ("off", False)],
)
def test_boolean_parsing(raw: str, expected: bool) -> None:
    s = load(ALLOW_WRITES=raw, LOGIN_DISABLED=raw, DRY_RUN=raw)
    assert (s.allow_writes, s.login_disabled, s.dry_run) == (expected, expected, expected)


@pytest.mark.parametrize("raw", ["maybe", "", "2", "tru"])
def test_bad_boolean_rejected(raw: str) -> None:
    with pytest.raises(ConfigError, match="EASYSMART_VERIFY_TLS"):
        load(VERIFY_TLS=raw)


@pytest.mark.parametrize(
    "host", ["sw.example.internal", "sw-1.example.com", DOC_HOST, "2001:db8::1", "SW"]
)
def test_valid_hosts(host: str) -> None:
    assert load(HOST=host).host == host


def test_ipv6_base_url_is_bracketed() -> None:
    assert load(HOST="2001:db8::1").base_url == "https://[2001:db8::1]:443"


@pytest.mark.parametrize(
    "host",
    [
        "   ",
        "https://" + DOC_HOST,
        DOC_HOST + "/path",
        DOC_HOST + ":443",
        "bad host",
        "host\x00null",
        "-leading-dash",
        "a" * 254,
        "under_score.example",
        "<sw-host>",
        "999.1.1.1",
    ],
)
def test_invalid_hosts_rejected(host: str) -> None:
    with pytest.raises(ConfigError, match="EASYSMART_HOST"):
        load(HOST=host)


def test_host_is_trimmed() -> None:
    assert load(HOST=f"  {DOC_HOST}  ").host == DOC_HOST


@pytest.mark.parametrize("port", ["0", "70000", "-1", "65536", "nan", "NaN", "inf", "1.5", "abc"])
def test_invalid_ports_rejected(port: str) -> None:
    with pytest.raises(ConfigError):
        load(PORT=port)
    with pytest.raises(ConfigError):
        load_global_settings(MCP_PREFIX, {f"{MCP_PREFIX}PORT": port})


@pytest.mark.parametrize("port", ["1", "80", "65535"])
def test_valid_port_boundaries(port: str) -> None:
    assert load(PORT=port).port == int(port)


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0", "0.5", "121"])
def test_invalid_timeout_rejected(timeout: str) -> None:
    with pytest.raises(ConfigError):
        load(TIMEOUT_SECONDS=timeout)


def test_missing_password_fails_fast() -> None:
    with pytest.raises(ConfigError, match="EASYSMART_PASSWORD"):
        load_device_settings(PREFIX, {f"{PREFIX}HOST": DOC_HOST})


@pytest.mark.parametrize("password", ["", "x" * 129, "bad\x00pw", "tab\tpw"])
def test_invalid_password_rejected(password: str) -> None:
    with pytest.raises(ConfigError):
        load(**{"PASSWORD": password})


def test_config_error_never_echoes_password() -> None:
    secret = "S3cret-" + "x" * 130
    with pytest.raises(ConfigError) as info:
        load(**{"PASSWORD": secret})
    assert "S3cret" not in str(info.value)


def test_password_hidden_in_repr() -> None:
    s = load()
    assert TEST_PASSWORD not in repr(s)
    assert TEST_PASSWORD not in str(s.model_dump())


@pytest.mark.parametrize("username", ["", "a" * 65, "ad\nmin", " "])
def test_invalid_username_rejected(username: str) -> None:
    with pytest.raises(ConfigError):
        load(USERNAME=username)


@pytest.mark.parametrize("value", ["0", "6", "nan"])
def test_max_login_failures_bounds(value: str) -> None:
    with pytest.raises(ConfigError):
        load(MAX_LOGIN_FAILURES=value)


def test_settings_are_immutable() -> None:
    s = load()
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        s.port = 1  # type: ignore[misc]


def test_extra_fields_rejected_on_direct_construction() -> None:
    with pytest.raises(Exception):  # noqa: B017
        DeviceSettings.model_validate(
            {"env_prefix": PREFIX, "host": DOC_HOST, "password": "x", "bogus": 1}
        )


# ---- global (MCP) settings ----------------------------------------------------


def test_global_defaults() -> None:
    g = load_global_settings(MCP_PREFIX, {})
    assert (g.mcp_transport, g.mcp_host, g.mcp_port) == ("stdio", "127.0.0.1", 8765)
    assert g.mcp_host_is_loopback is True


def test_global_transport_choice() -> None:
    assert (
        load_global_settings(
            MCP_PREFIX, {f"{MCP_PREFIX}TRANSPORT": "streamable-http"}
        ).mcp_transport
        == "streamable-http"
    )
    with pytest.raises(ConfigError, match="EASYSMART_MCP_TRANSPORT"):
        load_global_settings(MCP_PREFIX, {f"{MCP_PREFIX}TRANSPORT": "sse"})


@pytest.mark.parametrize("bind", ["localhost", "not an ip", ""])
def test_mcp_host_must_be_ip_literal(bind: str) -> None:
    with pytest.raises(ConfigError):
        load_global_settings(MCP_PREFIX, {f"{MCP_PREFIX}HOST": bind})


def test_non_loopback_bind_is_flagged() -> None:
    assert (
        load_global_settings(MCP_PREFIX, {f"{MCP_PREFIX}HOST": "0.0.0.0"}).mcp_host_is_loopback  # noqa: S104
        is False
    )

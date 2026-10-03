"""Configuration for one TP-Link Easy Smart switch (prefix ``EASYSMART_``).

``SwitchSettings`` extends the device-agnostic ``core.config.DeviceSettings`` and
adds the switch-specific policy: which ports carry PoE, which ports must never be
touched, the friendly port-name map, the state directory for the breaker and
cooldown, and the power-cycle bounds. Everything is validated once at startup so a
malformed value never reaches the device (a bad password would trip the switch's
own lockout).

Transport is plain HTTP on :80 — the switch has no TLS — so ``VERIFY_TLS`` and
``TLS_FINGERPRINT_SHA256`` are rejected outright rather than silently ignored.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from ..core.config import DeviceSettings, Port, reject_unknown_env
from ..core.errors import ConfigError

log = logging.getLogger(__name__)

# suffix -> model field. This is the complete set of accepted EASYSMART_* keys;
# anything else (including a typo) is rejected loudly by the loader.
SWITCH_ENV_SUFFIXES: dict[str, str] = {
    "HOST": "host",
    "PORT": "port",
    "USERNAME": "username",
    "PASSWORD": "password",
    "ALLOW_WRITES": "allow_writes",
    "LOGIN_DISABLED": "login_disabled",
    "DRY_RUN": "dry_run",
    "TIMEOUT_S": "timeout_seconds",
    "POE_PORTS": "poe_ports",
    "PROTECTED_PORTS": "protected_ports",
    "PORT_MAP": "port_map",
    "STATE_DIR": "state_dir",
    "LOGIN_COOLDOWN_S": "login_cooldown_s",
    "CYCLE_OFF_MIN_S": "cycle_off_min_s",
    "CYCLE_OFF_MAX_S": "cycle_off_max_s",
    "CYCLE_POWER_TIMEOUT_S": "cycle_power_timeout_s",
    "LOGOUT_AFTER_READS": "logout_after_reads",
}
# Rejected on sight: the switch is HTTP-only, so these cannot do anything useful.
_TLS_SUFFIXES = ("VERIFY_TLS", "TLS_FINGERPRINT_SHA256")

ENV_PREFIX = "EASYSMART_"
MAX_PORT = 64  # hard sanity bound; the live max_port_num is checked at runtime
# Parsing charset (rejects unicode / control chars / separators). Leading digits
# are permitted here so a name can be parsed and then rejected with a clear error.
_PORT_NAME = re.compile(r"[A-Za-z0-9 _.-]{1,32}")
# The strict rule enforced at the env boundary: a name must start with a letter and
# never be purely numeric, so an int and a str of the same digits can never both be
# a valid handle for different things (breaker finding TC-F5).
_STRICT_PORT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9 _.-]{0,31}")
_DEFAULT_STATE_DIR = str(Path.home() / ".local" / "state" / "tplink-easysmart-mcp")


def _parse_port_set(spec: object, *, field: str) -> tuple[int, ...]:
    """Parse ``"1-8"`` / ``"1,2,5-8"`` into a sorted tuple of ints. Empty ⇒ ``()``."""
    if isinstance(spec, tuple):
        return spec
    if not isinstance(spec, str):
        raise ValueError(f"{field}: must be a comma/range string")
    text = spec.strip()
    if not text:
        return ()
    ports: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            raise ValueError(f"{field}: empty entry (stray comma)")
        if "-" in token.lstrip("-"):  # a range like 5-8 (not a lone negative)
            lo_s, _, hi_s = token.partition("-")
            lo, hi = _port_int(lo_s, field), _port_int(hi_s, field)
            if lo > hi:
                raise ValueError(f"{field}: reversed range {token!r}")
            ports.update(range(lo, hi + 1))
        else:
            ports.add(_port_int(token, field))
    return tuple(sorted(ports))


def _port_int(raw: str, field: str) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise ValueError(f"{field}: {raw!r} is not a port number")
    value = int(raw)
    if not 1 <= value <= MAX_PORT:
        raise ValueError(f"{field}: port {value} out of range 1..{MAX_PORT}")
    return value


def _parse_port_map(spec: object) -> tuple[tuple[str, int], ...]:
    """Parse ``"name=port;name=port"`` into sorted ``(lowercased_name, port)`` pairs."""
    if isinstance(spec, tuple):
        return spec
    if not isinstance(spec, str):
        raise ValueError("PORT_MAP: must be a 'name=port;...' string")
    text = spec.strip()
    if not text:
        return ()
    seen_names: dict[str, int] = {}
    seen_ports: dict[int, str] = {}
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            raise ValueError("PORT_MAP: empty entry (stray or trailing ';')")
        name, sep, port_s = entry.partition("=")
        name = name.strip()
        if not sep:
            raise ValueError(f"PORT_MAP: entry {entry!r} is not name=port")
        if not _PORT_NAME.fullmatch(name):
            raise ValueError(f"PORT_MAP: name {name!r} is not [A-Za-z0-9_.-]{{1,32}}")
        key = name.lower()
        if key in seen_names:
            raise ValueError(f"PORT_MAP: duplicate name {name!r} (names are case-insensitive)")
        port = _port_int(port_s, "PORT_MAP")
        if port in seen_ports:
            log.warning("PORT_MAP: port %d mapped by both %r and %r", port, seen_ports[port], name)
        seen_names[key] = port
        seen_ports[port] = name
    return tuple(sorted(seen_names.items()))


class SwitchSettings(DeviceSettings):
    port: Port = 80
    timeout_seconds: float = Field(default=5.0, ge=1.0, le=120.0, allow_inf_nan=False)
    poe_ports: tuple[int, ...] = tuple(range(1, 9))
    protected_ports: tuple[int, ...] = ()
    port_map: tuple[tuple[str, int], ...] = ()
    state_dir: str = _DEFAULT_STATE_DIR
    login_cooldown_s: int = Field(default=300, ge=1, le=86_400)
    cycle_off_min_s: int = Field(default=5, ge=1, le=3_600)
    cycle_off_max_s: int = Field(default=120, ge=1, le=3_600)
    cycle_power_timeout_s: int = Field(default=60, ge=1, le=3_600)
    logout_after_reads: bool = False

    @field_validator("password")
    @classmethod
    def _switch_password_bounds(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 6 <= len(raw) <= 16:
            raise ValueError("must be 6-16 characters (the switch's own limit)")
        if " " in raw:
            raise ValueError("must not contain spaces")
        return value

    @field_validator("poe_ports", mode="before")
    @classmethod
    def _parse_poe(cls, value: object) -> tuple[int, ...]:
        return _parse_port_set(value, field="POE_PORTS")

    @field_validator("protected_ports", mode="before")
    @classmethod
    def _parse_protected(cls, value: object) -> tuple[int, ...]:
        return _parse_port_set(value, field="PROTECTED_PORTS")

    @field_validator("port_map", mode="before")
    @classmethod
    def _parse_map(cls, value: object) -> tuple[tuple[str, int], ...]:
        return _parse_port_map(value)

    @model_validator(mode="after")
    def _writes_need_guards(self) -> SwitchSettings:
        if self.allow_writes:
            if not self.protected_ports:
                raise ValueError(
                    "PROTECTED_PORTS must be set and non-empty when ALLOW_WRITES=true "
                    "(name the uplink and the server's own port so they can never be disabled)"
                )
            if not self.poe_ports:
                raise ValueError("POE_PORTS must be non-empty when ALLOW_WRITES=true")
        if self.cycle_off_min_s > self.cycle_off_max_s:
            raise ValueError("CYCLE_OFF_MIN_S must not exceed CYCLE_OFF_MAX_S")
        return self

    @property
    def base_url(self) -> str:
        # Plain HTTP only: the switch exposes no TLS (ports 22/23/443 are closed),
        # the login crosses the LAN in cleartext, and deployment is wired-LAN only.
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def referer(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}/"

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()

    @property
    def port_map_dict(self) -> dict[str, int]:
        return dict(self.port_map)

    def is_poe_port(self, port: int) -> bool:
        return port in self.poe_ports

    def is_protected(self, port: int) -> bool:
        return port in self.protected_ports

    def summary(self) -> dict[str, object]:
        """A secret-free view of the policy for ``switch_status`` (no password, no cookies)."""
        return {
            "host": self.host,
            "port": self.port,
            "allow_writes": self.allow_writes,
            "dry_run": self.dry_run,
            "login_disabled": self.login_disabled,
            "logout_after_reads": self.logout_after_reads,
            "poe_ports": list(self.poe_ports),
            "protected_ports": list(self.protected_ports),
            "port_map_names": [name for name, _ in self.port_map],
            "port_map": dict(self.port_map),
            "login_cooldown_s": self.login_cooldown_s,
        }


def poe_ports_mismatch(settings: SwitchSettings, poe_port_num: int) -> str | None:
    """Return a WARNING message if EASYSMART_POE_PORTS disagrees with the live count."""
    configured = set(settings.poe_ports)
    live = set(range(1, poe_port_num + 1))
    if configured == live:
        return None
    extra = sorted(configured - live)
    missing = sorted(live - configured)
    parts = []
    if extra:
        parts.append(f"configured PoE port(s) {extra} exceed poe_port_num={poe_port_num}")
    if missing:
        parts.append(f"switch PoE port(s) {missing} are not in EASYSMART_POE_PORTS")
    return "; ".join(parts)


def load_switch_settings(environ: dict[str, str]) -> SwitchSettings:
    """Build ``SwitchSettings`` from ``EASYSMART_*``. Fails fast with a clear message."""
    for suffix in _TLS_SUFFIXES:
        if ENV_PREFIX + suffix in environ:
            raise ConfigError(
                f"{ENV_PREFIX}{suffix} is not applicable: the switch is HTTP-only. Remove it."
            )
    # The server settings share this device's prefix (``EASYSMART_MCP_*``) and are
    # validated by ``load_global_settings``, so drop them before the device-level
    # unknown-key check (the canonical ``reject_unknown_env`` has no subprefix escape).
    device_environ = {k: v for k, v in environ.items() if not k.startswith(ENV_PREFIX + "MCP_")}
    reject_unknown_env(ENV_PREFIX, SWITCH_ENV_SUFFIXES, device_environ)
    raw: dict[str, object] = {
        field: environ[ENV_PREFIX + suffix]
        for suffix, field in SWITCH_ENV_SUFFIXES.items()
        if ENV_PREFIX + suffix in environ
    }
    field_to_env = {field: ENV_PREFIX + suffix for suffix, field in SWITCH_ENV_SUFFIXES.items()}
    try:
        settings = SwitchSettings.model_validate({**raw, "env_prefix": ENV_PREFIX})
    except ValidationError as exc:
        raise ConfigError(_format(exc, field_to_env)) from None
    _reject_numeric_port_names(settings)
    return settings


def _reject_numeric_port_names(settings: SwitchSettings) -> None:
    """A ``PORT_MAP`` name must start with a letter and never be purely numeric.

    Otherwise a numeric name could shadow a literal port number (TC-F5): the same
    token resolving to two different physical ports by argument type alone.
    """
    bad = [name for name, _ in settings.port_map if not _STRICT_PORT_NAME.fullmatch(name)]
    if bad:
        raise ConfigError(
            f"{ENV_PREFIX}PORT_MAP: name(s) {bad} are invalid; a name must start with a letter "
            "and match [A-Za-z][A-Za-z0-9 _.-]{0,31} (purely numeric names and names starting "
            "with a digit are refused so they cannot shadow a literal port number)."
        )


def _format(exc: ValidationError, field_to_env: dict[str, str]) -> str:
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        field = str(err["loc"][0]) if err["loc"] else "configuration"
        name = field_to_env.get(field, field)
        msg = str(err["msg"]).removeprefix("Value error, ")
        lines.append(f"{name}: {msg}")
    return "Invalid configuration: " + "; ".join(lines)

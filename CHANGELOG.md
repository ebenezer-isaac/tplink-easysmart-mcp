# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Live on-device verification (phases S0 / S4): confirm the `# S0: confirm`
  constants (auto power-limit field, array padding, disabled-PoE read value,
  `Logout.htm` behaviour, VLAN page names) on a real TL-SG1016PE, then tag 0.1.0.

## [0.1.0] — unreleased

First public release. Offline-complete: the full tool surface is implemented and
covered by an offline test suite against synthetic fixtures; on-device
verification is the only step outstanding.

### Added

- Browser-free MCP server for TP-Link Easy Smart switches (TL-SG1016PE target),
  speaking the HTML + `*.cgi` web protocol directly over cleartext HTTP.
- Thirteen `switch_`-prefixed tools returning a uniform `{success, data, error}`
  envelope:
  - Reads: `switch_status`, `switch_check_auth`, `switch_login`, `switch_logout`,
    `switch_get_system_info`, `switch_get_ports`, `switch_get_port_stats`,
    `switch_get_poe`, `switch_get_vlans`, `switch_resolve_port`.
  - Writes (double-gated): `switch_set_poe`, `switch_set_port`, `switch_poe_cycle`.
- Safety model: two-key write gate (`EASYSMART_ALLOW_WRITES` + `confirm_write`),
  dry-run, read-modify-write with verification, protected ports, single-session
  eviction handling, restored-account (factory-reset) refusal, a persistent
  lockout breaker with cooldown, and per-port crash-visible power-cycle
  reservations that restore power on every failure path.
- Pure inline-variable extractor (no `json5` dependency), structural page
  classification that resists reflected-string spoofing, and bounded parsers.
- CLI: `serve`, `--list-tools`, `check-auth [--login]`, `breaker --show|--clear`,
  with failure-kind exit codes.
- Configuration via `EASYSMART_*` environment variables with fail-fast validation
  (see `.env.example`); TLS variables rejected on this HTTP-only device.
- Docs: protocol notes, endpoint map, specs, example systemd unit with install
  guide, security policy, and contributor guide.

### Security

- Cleartext-credential threat model documented in `SECURITY.md`; deployment is
  restricted to the wired LAN host that reaches the switch.

[Unreleased]: https://github.com/ebenezer-isaac/tplink-easysmart-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ebenezer-isaac/tplink-easysmart-mcp/releases/tag/v0.1.0

# tplink-easysmart-mcp

A local, browser-free [MCP](https://modelcontextprotocol.io) server for the
**TP-Link TL-SG1016PE Easy Smart PoE switch**. Read-first, write-gated, and
lockout-aware: it never retries a failed login, gates every write behind two
keys, and refuses to act in the switch's factory-reset mode.

> **Status:** early build. Phase S1 ships the pure page parsers, form builders
> and login-page classifier plus the `switch_status` healthcheck. The
> authenticated client, typed read tools and PoE power-cycle tool land in the
> following phases.

## Why

The Easy Smart line has no JSON API: it serves HTML pages with inline
`var NAME = VALUE;` state and takes `*.cgi` form posts, with a **cleartext**
login. This server speaks that protocol directly — no headless browser at
runtime — so an MCP client can read port/PoE status and power-cycle a PoE
camera without anyone opening the switch's web UI.

## Safety model

- **Cleartext on the LAN.** Run it only on the wired LAN host that reaches the
  switch. The HTTP transport binds to loopback; reach it over Tailscale/SSH.
- **Writes need two keys.** `EASYSMART_ALLOW_WRITES=true` on the server **and**
  `confirm_write=true` on the call. Otherwise a mutating tool performs no
  network call.
- **Single effective session.** Logging in evicts the owner's web-UI session and
  vice versa. The server logs in lazily, re-logs in at most once per read, and
  logs out after every write.
- **Restored-account guard.** In factory-reset mode a login POST would *set* the
  admin password; the server refuses to POST and trips the breaker.
- **Protected ports.** `EASYSMART_PROTECTED_PORTS` (required when writes are on)
  blocks changes to the uplink and the server's own port.

## Configuration

Copy `.env.example` and fill it in. Every `EASYSMART_*` variable is documented
there. The switch address, password and port map live only in that file.

## Development

```
pip install -e '.[dev]'
python scripts/gate.py      # ruff + pytest (coverage) + secret scan + stub scan + --list-tools
```

Protocol notes: `docs/protocol/easysmart-switch.md`. Specs: `docs/specs/`.

## Credits

Request and parsing logic ported from
[`vmakeev/hass_tplink_easy_smart`](https://github.com/vmakeev/hass_tplink_easy_smart)
(MIT, © 2022 Vladimir Makeev). See `NOTICE`.

## License

MIT — see `LICENSE`.

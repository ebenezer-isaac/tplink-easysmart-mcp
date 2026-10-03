# tplink-easysmart-mcp

A local, browser-free [MCP](https://modelcontextprotocol.io) server for the
**TP-Link TL-SG1016PE Easy Smart PoE switch**. Read-first, write-gated, and
lockout-aware: it never retries a failed login, gates every write behind two
keys, and refuses to act in the switch's factory-reset mode.

> **Status:** the authenticated client, the typed read tools and the write /
> PoE power-cycle tools are in. Live on-device verification (phase S4) is pending.

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

## Tools

All tools return the `{success, data, error}` envelope and never raise. A port
argument is an `int` **or** a case-insensitive `EASYSMART_PORT_MAP` name
(`switch_resolve_port` previews the mapping).

| Tool | Mutates | What it does / refuses |
|---|---|---|
| `switch_status` | no | Healthcheck: config summary (writes, dry-run, PoE/protected ports, port map), one credential-free reachability probe, breaker/cooldown/cycle-marker state. Never logs in. |
| `switch_check_auth` | no | Probe only (one GET): session model, auth variant, login mode. |
| `switch_login` | no | Exactly one login, confirm, then logout. Returns hw/fw; never cookies. |
| `switch_logout` | no | End any lingering session (no-op if not logged in). |
| `switch_get_system_info` | no | Model, hw revision, firmware, MAC, IP, netmask, gateway, session model. |
| `switch_get_ports` | no | Per-port state, link, speed, flow control, LAG, name, protected. `only_linked` filters. |
| `switch_get_port_stats` | no | tx/rx good/bad counters for one port or all, plus `error_ports`. |
| `switch_get_poe` | no | Budget totals + per-port state/priority/limit/class/W/mA/V/status, plus `fault_ports` and `unpowered_enabled_ports`. |
| `switch_get_vlans` | no | 802.1Q table + per-port PVIDs, or `NOT_SUPPORTED` on firmware without it. |
| `switch_resolve_port` | no | Resolve a name/number to `{port, name, is_poe, protected, max_port}`. |
| `switch_set_poe` | **yes** | Enable/disable PoE on one port (read-modify-write; verifies priority/limit survive). Refuses `NOT_POE_PORT`, `PROTECTED_PORT`. |
| `switch_set_port` | **yes** | Enable/disable one port's link (RMW; verifies speed/flow-control survive). Refuses `PROTECTED_PORT`. |
| `switch_poe_cycle` | **yes** | Power-cycle a PoE camera: off → wait `off_seconds` → on → wait for power. Refuses `NOT_POE_PORT`, `PROTECTED_PORT`, `ALREADY_OFF`, `CYCLE_IN_PROGRESS`; reports `CYCLE_INCOMPLETE`/`POWER_NOT_RESTORED` naming any port left UNPOWERED. |

### Write gating

Every mutating tool needs **two keys**: `EASYSMART_ALLOW_WRITES=true` on the
server **and** `confirm_write=true` on the call. Miss either and the tool returns
a refusal **before any network call**. `EASYSMART_DRY_RUN=true` makes a write read
the live page and return the exact form it *would* send, sending nothing. Every
write (and every power-cycle) logs out afterwards, on success and on failure.

### Protected ports (required for writes)

`EASYSMART_PROTECTED_PORTS` must be set and non-empty whenever writes are enabled
(startup fails otherwise). List the uplink and the server's own port; no write or
power-cycle will touch them.

### Power-cycle safety

Only one cycle runs at a time: an in-process lock refuses a concurrent call, and a
state-file marker under `EASYSMART_STATE_DIR` makes a crashed cycle visible (a
marker younger than 5 minutes refuses a new cycle; an older one is reported stale
and overwritten). Once the port is off, every failure path still attempts to turn
it back on and reports both outcomes.

### Restored-account guard

In factory-reset mode the login form becomes "New Password / Confirm" and a POST
would *set* the admin password. The server refuses to POST and trips the breaker.

### Session eviction

The switch is effectively single-session: **the switch web UI is logged out
whenever the MCP acts, and vice versa.** Reads log in lazily and re-log in at most
once per call; writes log out when done.

### Deployment

Cleartext login crosses the LAN, so run this only on the wired-LAN host that
reaches the switch, bound to loopback and reached over Tailscale/SSH.

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

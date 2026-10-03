# tplink-easysmart-mcp

A local, browser-free [MCP](https://modelcontextprotocol.io) server for the
**TP-Link Easy Smart** family of managed PoE switches (developed against the
**TL-SG1016PE**). It speaks the switch's HTML/`*.cgi` web protocol directly — no
headless browser at runtime — so an MCP client (Claude, or any MCP host) can read
port, statistics and PoE status and **power-cycle a wedged PoE camera** without
anyone opening the switch's web UI.

It is read-first, write-gated and lockout-aware: it never retries a failed login,
gates every write behind two independent keys, restores power on every failure
path, and refuses to act when the switch is in factory-reset mode.

> **Status: 0.1.0.** The authenticated client, the typed read tools and the
> write / PoE power-cycle tools are implemented and covered by an offline test
> suite against synthetic fixtures. Live on-device verification (phase S4) is the
> last step before a tagged release; see [`docs/specs/`](docs/specs/).

## Why

The Easy Smart line has no JSON API. Each page is HTML with an inline
`var NAME = VALUE;` state block, and changes are made with `*.cgi` form posts over
a **cleartext** HTTP login on port 80 — there is no TLS on the device. This server
re-implements that protocol as a set of typed, safety-gated MCP tools, so an agent
can answer "is the gate camera drawing power?" or "the hallway camera is frozen,
power-cycle it" over the LAN.

## Supported hardware

| Hardware | Status |
|---|---|
| **TL-SG1016PE** V1 / V3 (16-port, PoE+ on 1–8) | Primary target. Protocol logic matches the MIT reference client, which lists V1 and V3 as fully supported including PoE. Live capture (S0) pending. |
| Other Easy Smart models (SG108E, SG105E, SG1016PE siblings, SG1428PE, …) | **Likely to work** — they share the page names, inline-variable layout and PoE encodings — but each needs an S0 live capture to confirm array padding, the auto power-limit field and the VLAN page names before it is called "tested". |
| Newer **encrypted-login** firmware (salted-XOR password + `g_tid` token) | **Refused, fail-closed.** The login probe detects the encrypted variant and raises `AUTH_VARIANT_UNSUPPORTED` before any POST. Port it if a firmware update brings it; the markers are already detected. |

The switch's hardware revision and firmware are only readable after login, so for
an unknown unit run `tplink-easysmart-mcp check-auth --login` once and compare.

## Features

Thirteen tools, all prefixed `switch_`, all returning the same
`{success, data, error}` envelope and never raising:

- **System info** — model, hardware revision, firmware, MAC, IP, netmask, gateway.
- **Ports** — per-port admin state, link, speed, flow control, LAG, name, protected flag.
- **Statistics** — tx/rx good/bad packet counters, with derived `error_ports`.
- **PoE** — budget totals and per-port state/priority/limit/class/W/mA/V/status,
  with derived `fault_ports` and `unpowered_enabled_ports`.
- **VLANs** — the 802.1Q table and per-port PVIDs (or `NOT_SUPPORTED`).
- **Resolve port** — preview how a name or number maps to a physical port.
- **Writes (gated):** `switch_set_poe`, `switch_set_port`, and `switch_poe_cycle`
  (power-cycle a PoE camera off → wait → on → confirm power).

## Install

Requires Python 3.11+.

```bash
pip install git+https://github.com/ebenezer-isaac/tplink-easysmart-mcp
# or, from a clone:
pip install .
```

Register it with your MCP client (stdio transport) — for example in a Claude
Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "tplink-switch": {
      "command": "tplink-easysmart-mcp",
      "env": {
        "EASYSMART_HOST": "192.0.2.10",
        "EASYSMART_PASSWORD": "your-switch-password",
        "EASYSMART_PORT_MAP": "cam_hall=1;cam_gate=2"
      }
    }
  }
}
```

Leave `EASYSMART_ALLOW_WRITES` unset (the default) to run read-only. See the next
section for every variable.

## Configuration

Copy [`.env.example`](.env.example) and fill it in, or pass the variables through
your MCP client's `env`. The switch address, password and port map live only in
that file — never in the repo.

| Variable | Default | Meaning |
|---|---|---|
| `EASYSMART_HOST` | — (required) | Switch IP or hostname. Scheme is always `http`. |
| `EASYSMART_PORT` | `80` | HTTP port. |
| `EASYSMART_USERNAME` | `admin` | 1–16 printable-ASCII chars. |
| `EASYSMART_PASSWORD` | — (required) | **6–16 chars, no spaces** (the switch's own limit). Validated at startup so a bad value never reaches the device and trips its lockout. |
| `EASYSMART_TIMEOUT_S` | `5` | Per-request timeout (1–120). The web server is single-threaded; keep it short. |
| `EASYSMART_ALLOW_WRITES` | `false` | Master write switch. Half of the write gate. |
| `EASYSMART_DRY_RUN` | `false` | A write reads the live page and returns the exact form it *would* send, sending nothing. |
| `EASYSMART_LOGIN_DISABLED` | `false` | Freezes authentication: no login attempt is ever made. |
| `EASYSMART_STATE_DIR` | `~/.local/state/tplink-easysmart-mcp` | Where the breaker, cooldown and cycle-reservation files live. |
| `EASYSMART_POE_PORTS` | `1-8` | PoE-capable ports (ranges and commas, e.g. `1,2,5-8`). A PoE write requires the port to be here **and** ≤ the live `poe_port_num`. |
| `EASYSMART_PROTECTED_PORTS` | — | Ports no write may ever touch: the uplink and the server's own port. **Required and non-empty whenever `ALLOW_WRITES=true`** (startup fails otherwise). E.g. `16,15`. |
| `EASYSMART_PORT_MAP` | — | Friendly names: `name=port;name=port`. A name must **start with a letter** and match `[A-Za-z][A-Za-z0-9 _.-]{0,31}`; purely numeric names are refused so a name can never shadow a port number. Case-insensitive, must be unique. |
| `EASYSMART_LOGIN_COOLDOWN_S` | `300` | Minimum gap between a failed/unconfirmed login and the next attempt. |
| `EASYSMART_CYCLE_OFF_MIN_S` / `EASYSMART_CYCLE_OFF_MAX_S` | `5` / `120` | Bounds for a power-cycle's `off_seconds` (an out-of-range value is refused, not clamped). |
| `EASYSMART_CYCLE_POWER_TIMEOUT_S` | `60` | How long to wait for `powerstatus == on` after re-enabling a port. |
| `EASYSMART_LOGOUT_AFTER_READS` | `false` | If true, read tools also log out. Use it when a human works in the switch web UI often (see session eviction below). |
| `EASYSMART_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `EASYSMART_MCP_HOST` / `EASYSMART_MCP_PORT` | `127.0.0.1` / `8766` | HTTP transport bind. Keep it on loopback; the server has no auth of its own. |

`VERIFY_TLS` / `TLS_FINGERPRINT_SHA256` are **rejected** if set — the device is
HTTP-only, so they could do nothing but mislead.

## Running

```bash
tplink-easysmart-mcp                      # serve over EASYSMART_MCP_TRANSPORT
tplink-easysmart-mcp --list-tools         # print the 13 tool names; no device I/O
tplink-easysmart-mcp check-auth           # probe only (one GET, no login)
tplink-easysmart-mcp check-auth --login   # exactly one login, confirm, then log out
tplink-easysmart-mcp breaker --show       # print the persistent breaker state
tplink-easysmart-mcp breaker --clear      # clear the breaker after fixing the cause
```

## Safety model

This device makes safety the whole point of the project. See
[`SECURITY.md`](SECURITY.md) for the full threat model.

- **Cleartext login → LAN host only.** The password crosses the wire in cleartext
  on every login. **Run this server on the wired LAN host that reaches the switch
  — never across WiFi or a Tailscale/SSH hop.** The HTTP transport binds to
  loopback; reach the *MCP* over Tailscale/SSH, but keep the switch traffic local.
- **Writes are double-gated.** A mutating tool acts only when
  `EASYSMART_ALLOW_WRITES=true` on the server **and** `confirm_write=true` on the
  call. Miss either and the tool returns a refusal **before any network call**.
- **Dry-run.** `EASYSMART_DRY_RUN=true` makes a write read the live page and return
  the exact bytes it would send, sending nothing.
- **Read-modify-write + verify.** Every write reads the current page, changes only
  the one field, re-sends the rest verbatim, then re-reads to confirm — so a PoE
  on/off never silently resets priority or limit (`WRITE_VERIFY_FAILED` if it did).
- **Protected ports.** `EASYSMART_PROTECTED_PORTS` (required when writes are on)
  names the uplink and the server's own port; no write or power-cycle touches them.
- **Single-session eviction.** The switch is effectively single-session: logging in
  evicts a human's web-UI session and vice versa. The server logs in lazily,
  re-logs in at most once per read, and logs out after every write.
- **Restored-account / factory-reset refusal.** In factory-reset mode a login POST
  would *set* the admin password. The server detects the "New Password / Confirm"
  page, refuses to POST, and trips the breaker.
- **Lockout breaker + cooldown.** A failed login (bad credentials, locked account,
  restored mode) trips a persistent breaker that blocks further logins until a
  human runs `breaker --clear`; session-busy / timeout responses set a cooldown
  instead. The login is **never** retried automatically.
- **One cycle per port, crash-visible.** Only one power-cycle runs per port at a
  time, reserved through a cross-process state file so a crashed cycle stays
  visible. Once the port is off, **every failure path still tries to turn it back
  on** and reports both outcomes; power is restored on every path.
- **Exit codes** (for `check-auth` / `breaker` in scripts): `0` success · `1` auth
  failed · `2` config error · `3` breaker open / cooldown / login disabled / state
  unavailable · `4` transport error.

## Tools

A port argument is an `int` **or** a case-insensitive `EASYSMART_PORT_MAP` name
(`switch_resolve_port` previews the mapping; resolution is type-stable, so `3` and
`"3"` are always the same physical port).

| Tool | R/W | What it does / refuses |
|---|---|---|
| `switch_status` | R | Healthcheck: config summary, one credential-free reachability probe, breaker/cooldown/cycle state. Never logs in. |
| `switch_check_auth` | R | Probe only (one GET): session model, auth variant, login mode. |
| `switch_login` | R | Exactly one login, confirm, then logout. Returns hw/fw; never cookies. |
| `switch_logout` | R | End any lingering session (no-op if not logged in). |
| `switch_get_system_info` | R | Model, hw revision, firmware, MAC, IP, netmask, gateway, session model. |
| `switch_get_ports` | R | Per-port state, link, speed, flow control, LAG, name, protected. `only_linked` filters. |
| `switch_get_port_stats` | R | tx/rx good/bad counters for one port or all, plus `error_ports`. |
| `switch_get_poe` | R | Budget totals + per-port state/priority/limit/class/W/mA/V/status, plus `fault_ports` and `unpowered_enabled_ports`. |
| `switch_get_vlans` | R | 802.1Q table + per-port PVIDs, or `NOT_SUPPORTED` on firmware without it. |
| `switch_resolve_port` | R | Resolve a name/number to `{port, name, is_poe, protected, max_port}`. |
| `switch_set_poe` | **W** | Enable/disable PoE on one port (RMW; verifies priority/limit survive). Refuses `NOT_POE_PORT`, `PROTECTED_PORT`. |
| `switch_set_port` | **W** | Enable/disable one port's link (RMW; verifies speed/flow-control survive). Refuses `PROTECTED_PORT`. |
| `switch_poe_cycle` | **W** | Power-cycle a PoE camera: off → wait `off_seconds` → on → wait for power. Refuses `NOT_POE_PORT`, `PROTECTED_PORT`, `ALREADY_OFF`, `CYCLE_IN_PROGRESS`; reports `CYCLE_INCOMPLETE`/`POWER_NOT_RESTORED` naming any port left UNPOWERED. |

## Recipes

**Power-cycle a wedged camera by name.** With writes enabled and the camera in
`EASYSMART_PORT_MAP`:

```
switch_poe_cycle(port_or_name="cam_hall", off_seconds=10, confirm_write=true)
```

It turns PoE off, waits, turns it back on, and polls until the camera draws power
again — restoring power and reporting the outcome even if a step fails. Dry-run it
first (`EASYSMART_DRY_RUN=true`) to see the exact off/on forms.

**Check the PoE budget.**

```
switch_get_poe()
```

Returns the system budget (limit / consumption / remaining) and every port's
draw, plus `fault_ports` (overload/short/voltage/thermal) and
`unpowered_enabled_ports` (PoE enabled but no power — often a dead or unplugged PD).

## Limitations

- **Live verification pending.** The protocol is implemented from the MIT
  reference client and synthetic fixtures; the on-device S0 capture and S4 cycle
  test have not been run. Items a live unit must confirm are tagged `# S0: confirm`
  in `switch/constants.py` (the auto power-limit wire value, the per-port array
  padding, the disabled-PoE read value, whether `Logout.htm` ends the session, and
  whether the 802.1Q pages exist under these names).
- **VLAN writes are not exposed.** The VLAN *read* layout is inferred from sibling
  models; VLAN writes have two incompatible firmware shapes and are intentionally
  not implemented.
- **Cable diagnostics are unknown.** No reference client implements the cable-test
  page; it is not a tool.
- **Hardware revision needs a login.** It is only on `SystemInfoRpm.htm`.

## Troubleshooting

- **`AUTH_FAILED` / `LOCKED_OUT` and the breaker is now open.** A login was
  rejected (errType 1/2). The server will not retry. Fix the credentials, then
  `tplink-easysmart-mcp breaker --clear`. Do **not** loop on login — the switch
  locks the admin account.
- **`LOGIN_NOT_ACCEPTED`.** The login POST returned errType 0 but the confirming
  GET came back as the login page (a known silent-ignore on cookie firmware). The
  credentials may be fine; a cooldown is recorded rather than tripping the breaker.
- **`SESSION_BUSY`.** Another process on your IP (usually a browser tab on the
  switch) already holds the session. Close it and retry.
- **`RESTORED_ACCOUNT_MODE`.** The switch is in factory-reset "set a new password"
  mode. Set the admin password in the web UI first; the server will not do it.
- **`AUTH_VARIANT_UNSUPPORTED`.** The firmware uses the encrypted login variant,
  which is not yet supported.
- **`OUTCOME_UNKNOWN`.** A write's connection was reset. The server re-reads rather
  than resending; check `switch_get_poe` / `switch_get_ports` for the real state.
- **`CYCLE_IN_PROGRESS` that never clears.** A previous cycle crashed and left a
  reservation. A marker older than its window is reported stale and overwritten;
  if it persists, remove the cycle marker under `EASYSMART_STATE_DIR`.
- **errType meanings:** 0 ok/silently-ignored · 1 bad credentials · 2 user blocked
  · 3/4 session slots full · 5 session timeout · 6 restored-account mode.

## Prior art and credits

- **[`vmakeev/hass_tplink_easy_smart`](https://github.com/vmakeev/hass_tplink_easy_smart)**
  (MIT, © 2022 Vladimir Makeev) — the request flow and the inline-variable / PoE /
  port parsing logic are **ported** from this Home Assistant integration (logic
  re-implemented, no files copied). See [`NOTICE`](NOTICE).
- **[`t0mer/SwitchDeck`](https://github.com/t0mer/SwitchDeck)** (Apache-2.0) — read
  for SG108E page/field names and connection-reset behaviour.
- **[`pklaus/smrt`](https://github.com/pklaus/smrt)** — reference for the Easy Smart
  web protocol and page conventions.

## Development

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# or: python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
python scripts/gate.py   # ruff + pytest (coverage) + secret scan + stub scan + --list-tools
```

`scripts/gate.py` must print `PASS` before any commit. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow, the breaker protocol, and
the core-identity rule (`core/` is canonical in `vigi-nvr-mcp` and copied verbatim).
Protocol notes live in
[`docs/protocol/easysmart-switch.md`](docs/protocol/easysmart-switch.md); the full
specs and endpoint map are in [`docs/specs/`](docs/specs/).

## License

[MIT](LICENSE). See [`NOTICE`](NOTICE) for third-party attribution.

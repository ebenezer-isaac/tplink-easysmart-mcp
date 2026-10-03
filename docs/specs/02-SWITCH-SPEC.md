# 02 — Easy Smart switch MCP (standalone repo `tplink-easysmart-mcp`)

**Repo:**
- Path: `E:/projects-working-dir/tplink-easysmart-mcp`.
- Package: `tplink_easysmart_mcp`.
- Console script: `tplink-easysmart-mcp`.
- License: MIT, **public**.

It is one of three **standalone** repos (owner decision); it is not part of a consolidated server. Like the other two, it shares no protocol code.

**Layout:**
- `tplink_easysmart_mcp/core/` is **copied verbatim** from `vigi-nvr-mcp/vigi_nvr_mcp/core/`, the self-contained device-agnostic template: config base, envelope, errors, redact, write_gate, breaker, serial, transport, server, cli. It is **not** re-implemented. The only permitted edits are import paths and the env prefix.
- Device code lives in `tplink_easysmart_mcp/switch/`.

**Shared conventions.** The tool prefix is `switch_`, plus `switch_status`. `scripts/gate.py`, `scripts/check_no_secrets.py`, the phase contract and the Phase Report format are those of `00-MASTER-PLAN.md` §1–§2. Read the two, with the master plan's tplink-local-mcp names read as this repo's names.

**Target.** TP-Link TL-SG1016PE Easy Smart PoE switch: 16 ports, PoE+ on ports 1–8. The hardware revision is unconfirmed: the login page is dated 2022, so it is probably V3 or later.

**Before you start.** Read `00-MASTER-PLAN.md` §1–§2 first; every rule there applies. The env prefix is `EASYSMART_`, and the state dir is `EASYSMART_STATE_DIR`, defaulting to `~/.local/state/tplink-easysmart-mcp/`.

**Where the protocol facts live.** In `docs/protocol/easysmart-switch.md`, which S1 writes from `switch-protocol.md`. S0's live findings override it.

**Inputs available to agents.** These sit in scratchpad `switch/`. The orchestrator copies what is needed into the repo before S1, and the copies **do not count** toward a phase's file budget.

| Input | Notes | Repo destination |
|---|---|---|
| `switch-protocol.md` | **Authoritative** until S0 amends it | — |
| `switch-endpoints.json` | Merged endpoint map, 29 entries plus 3 out of scope | `tplink_easysmart_mcp/switch/catalog/endpoints.json` |
| `fixtures/*.html` + `fixtures/MANIFEST.json` | 18 synthetic pages; `MANIFEST.json` holds the expected parse results | `tests/fixtures/` |
| S0 output (when delivered) | Sanitised live pages | `tests/fixtures/live/` |
| `S0-findings.md` (when delivered) | The S0 report | — |

**Credit.** The request and parsing logic is ported from `vmakeev/hass_tplink_easy_smart` (MIT, © 2022 Vladimir Makeev). Credit it in `NOTICE` and in the docstrings of `parsers.py`, `forms.py` and `auth.py`. **Do not copy its files.** Do not read or copy `tplinkrouterc6u` code (GPL); its page names are already in the endpoint map.

## Protocol (do not re-derive)

### Transport

- Plain HTTP on :80. HTTP status says nothing about auth: an unauthenticated request to any page returns **200 plus the login page**.
- Classify every body (`pages.classify`):
  - `LOGIN_PAGE`: contains `var logonInfo` or `action="/logon.cgi"`;
  - `DATA`: contains the expected anchor variable;
  - otherwise `UNEXPECTED`.
- Send `Referer: http://<host>/` on every request.
- Use a 5 s timeout.
- One request in flight at a time.

### Login

1. Clear the cookie jar, then `GET /`.
2. Probe the `GET /` body:
   - `session_model`: `cookie` if `Set-Cookie: H_P_SSID` is present, otherwise `ip_bound`. If the body is **not** a login page while the jar is empty, it is `ip_bound_active`: another process on our IP is logged in. Do not log in; raise `SESSION_BUSY`.
   - `auth_variant`: `plain_form`, `encrypted` or `unknown`. `encrypted` is detected by `encryptType`, `cryp_new.js`, `plain_password`, `g_tid`, `securityEncode` or RSA vars. `encrypted` and `unknown` mean fail closed with **no POST**.
   - `login_mode`: **`restored_account`** if `logonInfo[0] == 6`, or the body has `account_restored=1` or `value="Confirm"`. Otherwise `normal`.
3. `POST /logon.cgi` with body `username=<u>&password=<p>&cpassword=&logon=Login`. Send exactly one.
4. Read `errType = logonInfo[0]` from the response:

   | errType | Result |
   |---|---|
   | 1 | `AuthFailed` (bad credentials) |
   | 2 | `LockedOut` (user not allowed) |
   | 3, 4 | `SESSIONS_FULL` |
   | 5 | session timeout |
   | 6 | `RESTORED_ACCOUNT_MODE` |
   | 0 | **Ambiguous:** a success and a silently ignored POST look byte-identical (reference issue #49) |

5. **On errType 0, confirm** with `GET /SystemInfoRpm.htm`:
   - `DATA` means the session is established;
   - `LOGIN_PAGE` means `LOGIN_NOT_ACCEPTED`.
6. If the POST gets a connection reset, run the same confirm step. **Never re-POST.**

### Restored-account guard (hard)

- **Why.** In factory-reset mode the login form becomes "New Password / Confirm", and a POST sets the admin password.
- **The rule.** `forms.build_login_form(username, password, mode)` raises `RestoredAccountMode` for any `mode != LoginMode.NORMAL`. `cpassword` is always the empty string. No other code builds a logon body.

### Session

- **Model.** Either IP-bound or `H_P_SSID` cookie (Max-Age 600). The client is the same for both: `GET /` first, and keep a jar.
- **Treat it as single-session.** The owner's browser and the MCP evict each other (reference issues #2 and #7, both on TL-SG1016PE).
- **Rules:**
  - Log in lazily.
  - If a data GET returns `LOGIN_PAGE`, re-login **exactly once** and re-GET. A second `LOGIN_PAGE` means `SESSION_LOST`.
  - After every mutating tool, `GET /Logout.htm` and clear the jar.

### Reads

| Page | Variables |
|---|---|
| `SystemInfoRpm.htm` | `info_ds` (one-element arrays) |
| `PortSettingRpm.htm` | `max_port_num` and `all_info{state, trunk_info, spd_cfg, spd_act, fc_cfg, fc_act}`. Arrays are `max_port_num + 2` long. |
| `PortStatisticsRpm.htm` | `all_info{state, link_status, pkts}`. `pkts` holds 4 per port. |
| `PoeConfigRpm.htm` | `poe_port_num`, `portConfig{state, priority, powerlimit, power, current, voltage, pdclass, powerstatus}`, `globalConfig{system_power_*}` |
| `Vlan8021QRpm.htm` | `qvlan_ds` |
| `Vlan8021QPvidRpm.htm` | `pvid_ds` |

**Encodings** (switch-protocol §4):
- Power and voltage are in tenths.
- Priority is read as 0, 1, 2 (high, middle, low).
- `powerlimit` 330 means auto; 40, 70, 154 and 300 are the class presets.
- `pdclass` 40, 70, 154, 300 and 330 map to classes 1, 2, 3, 4 and 0. Anything else is `null` ("--").
- `powerstatus` is 0–9.

### Writes (read-modify-write only)

**PoE port.** `POST /poe_port_config.cgi` with body `sel_<n>=1&name_pstate=<2 on|1 off>&name_ppriority=<read+1>&name_ppowerlimit=<1 auto|2..5 class1-4|6 manual>&name_ppowerlimit2=<W | "(4w)"… | AUTO_LIMIT2>&applay=Apply`.
- `applay` is misspelt on the switch itself, and that misspelling is the real field name.
- Select exactly one port per request.

**Port.** `GET /port_setting.cgi?portid=<n>&state=<1|0>&speed=<current spd_cfg>&flowcontrol=<current fc_cfg>&apply=Apply`.

**Confirming a write.** Verify by re-reading the page. Treat a connection reset as "outcome unknown", then re-read. Never resend blindly.

## Configuration (`tplink_easysmart_mcp/switch/config.py`, extends `tplink_easysmart_mcp.core.config.DeviceSettings`, prefix `EASYSMART_`)

| Env var | Rule |
|---|---|
| `HOST`, `PORT` (80) | Scheme is always `http`. `VERIFY_TLS` and `TLS_FINGERPRINT_SHA256` are rejected if set. |
| `USERNAME` | 1–16 chars in `[\x21-\x7e]` |
| `PASSWORD` | 6–16 chars, no spaces. **Validated at startup**, so a malformed password never reaches the device and trips the breaker. |
| `ALLOW_WRITES`, `DRY_RUN`, `LOGIN_DISABLED`, `TIMEOUT_S` (5) | As in the master plan |
| `STATE_DIR` | Breaker and cooldown files. Default `~/.local/state/tplink-easysmart-mcp/`. |
| `POE_PORTS` (`"1-8"`) | PoE-capable ports. Accepts ranges and commas. A PoE write requires the port to be in this set **and** ≤ the live `poe_port_num`. A mismatch between the set and `poe_port_num` at first read logs a WARNING and is reported by `switch_status`. |
| `PROTECTED_PORTS` | A comma list, for example `"16,15"`: the uplink and the server's own port. **Required, and must be non-empty, when `ALLOW_WRITES=true`**; otherwise startup fails with `ConfigError`. |
| `PORT_MAP` | `name=port;name=port`. Names match `[A-Za-z0-9_.-]{1,32}` and are case-insensitively unique. Ports are 1–64; the real upper bound is checked at runtime against `max_port_num`. Duplicate ports are allowed only if names differ, with a warning. |
| `LOGIN_COOLDOWN_S` (300) | Minimum gap between a failed or unconfirmed login and the next attempt. Persisted (see S2). |
| `CYCLE_OFF_MIN_S` / `CYCLE_OFF_MAX_S` (5 / 120) | Bounds for `off_seconds` |
| `CYCLE_POWER_TIMEOUT_S` (60) | How long to wait for `powerstatus == on` after re-enable |
| `LOGOUT_AFTER_READS` (false) | If true, read tools also log out. Use it when the owner works in the web UI often. |

---

## Phase S0 — LIVE fixture capture (orchestrator-run, owner present)

**Preconditions:**
- No repo code is needed. This can run before S1.
- The owner is **logged out of the switch web UI**, and no browser tab is open on it.
- The password is in `~/.secrets/tplink-switch.pw` on the LAN server, mode 0600, written with `printf '%s'` so it has **no trailing newline**.
- Run the capture **on the LAN server only**. The password is cleartext on the wire.
- **Only `GET` `*.htm`. The only POST is the single `logon.cgi`. Fetch no other `*.cgi`, and no page whose name contains Reboot, Reset, Upgrade, Saving, Backup or Restore.**

**Commands** (bash; `H` = the switch address from the private env file):

```bash
umask 077; H=<switch-ip>; J=$(mktemp); OUT=~/switch-capture/$(date +%F); mkdir -p "$OUT"
C="curl -sS --max-time 5 -c $J -b $J -e http://$H/"
$C -D "$OUT/00-root.hdr" "http://$H/" -o "$OUT/00-root.html"
grep -i 'set-cookie' "$OUT/00-root.hdr"                                   # session_model
tr -d '\r\n' < "$OUT/00-root.html" | grep -o 'logonInfo = new Array([^)]*)'   # must be (0,0,0)
grep -Ec 'encryptType|cryp_new|plain_password|g_tid|securityEncode' "$OUT/00-root.html"  # must be 0
# STOP here if errType != 0 (6 = restored-account mode: never POST) or the variant count != 0.
$C --data-urlencode "username=admin" --data-urlencode "password@$HOME/.secrets/tplink-switch.pw" \
   --data "cpassword=&logon=Login" "http://$H/logon.cgi" -o "$OUT/01-logon.html"
tr -d '\r\n' < "$OUT/01-logon.html" | grep -o 'logonInfo = new Array([^)]*)'  # 1/2 -> STOP, do not retry
for p in SystemInfoRpm PortSettingRpm PortStatisticsRpm PoeConfigRpm Vlan8021QRpm Vlan8021QPvidRpm; do
  $C -D "$OUT/$p.hdr" "http://$H/$p.htm" -o "$OUT/$p.html"; done
$C "http://$H/" -o "$OUT/02-main.html"            # frameset, to discover the menu page name
# fetch the menu/frame pages named in 02-main.html (GET *.htm only, same denylist)
$C "http://$H/Logout.htm" -o "$OUT/03-logout.html"
$C "http://$H/SystemInfoRpm.htm" -o "$OUT/04-after-logout.html"   # must be the login page
rm -f "$J"
```

**Sanitise.** Use a scratch script **outside** the repo.
- Replace the switch MAC with `00:00:5E:00:53:10`, and any other MAC with `00:00:5E:00:53:xx`.
- Replace every IPv4 except `255.*` / `0.0.0.0` with `192.0.2.x`.
- Replace `descriStr` with `TL-SG1016PE`.
- Replace VLAN names with `vlanN`.
- Replace cookie values with `<redacted>`.
- Keep `hardwareStr` and `firmwareStr`.
- Run `scripts/check_no_secrets.py` over the result, then copy the result to `tests/fixtures/live/`.
- Raw captures stay in `~/switch-capture/` (0700) and are deleted after S4.

**Deliverables** (in the S0 report, `scratchpad/switch/S0-findings.md`; no repo code):
1. **Hardware and firmware:** `hardwareStr` and `firmwareStr`.
2. **Session:** `session_model` (cookie or IP-bound), the cookie's Max-Age if one is set, and whether `Logout.htm` ended the session (04 is the login page).
3. **Auth variant:** confirmed `plain_form`.
4. **Variable names:** for every page, the exact names and array lengths, compared with `fixtures/MANIFEST.json`. Specifically:
   - the padding (is it `max_port_num + 2`?);
   - the PoE array length;
   - the read value of a **disabled** PoE port's `state`. If no port is disabled, mark it "pending S4".
5. **The PoE form,** from `PoeConfigRpm.html` (all of it is inline JS): every field the page submits for each limit type. **This settles `AUTO_LIMIT2`** — empty, omitted, or `None` — and confirms the names `applay`, `name_p*` and `sel_<n>`.
6. **Menu:** the page names for save-config, reboot and cable diagnostics, plus whether the classic UI has an explicit "Save Config".
7. **Diff:** every difference from `switch-protocol.md`. The S1 agent applies these.

**Done when** the live fixtures are in the repo with `check_no_secrets` passing, `S0-findings.md` exists, and the owner has confirmed the web UI still works afterwards.

**If S0 is not yet possible.** S1 may proceed on the synthetic fixtures. Every parser constant that S0 must confirm sits in `tplink_easysmart_mcp/switch/constants.py`, and each one carries the comment `# S0: confirm`. These are a fact table, not stubs.

## Phase S1 — scaffold from the template, then pure parsers and form builders (one agent)

**Step 0: scaffold commit.** This is a separate commit, `chore: scaffold from vigi-nvr-mcp core`. It is **not counted** in the S1 budget, because it is a verbatim copy.

1. Copy the following into a fresh `git init` of `E:/projects-working-dir/tplink-easysmart-mcp`:
   - `vigi-nvr-mcp/vigi_nvr_mcp/core/` to `tplink_easysmart_mcp/core/`;
   - its tests to `tests/core/`;
   - `scripts/gate.py` and `scripts/check_no_secrets.py`;
   - `.gitignore`, `LICENSE`, `.env.example`, `deploy/*.service.example` and `pyproject.toml`.
2. Make only these edits:
   - the package name in imports;
   - the env prefix to `EASYSMART_`;
   - the console script to `tplink-easysmart-mcp`;
   - `server.py`'s global tool to `switch_status`, which returns the backend healthcheck plus config summary: `allow_writes`, `dry_run`, `protected_ports`, `poe_ports` and the port-map names. It must **never** return the password or cookies.
   - `.env.example` lists every `EASYSMART_*` var with placeholder values (`192.0.2.10`).
3. Before committing:
   - `python scripts/gate.py` passes, with core ≥ 90 % coverage carried over;
   - `diff -r` against the source core shows only those edits. Paste that diff in the Phase Report under `## Deviations`.

**Then the S1 deliverables** (≤ 13 files):

**`tplink_easysmart_mcp/switch/jsvars.py`.** `extract_vars(html) -> dict[str, JsValue]`:
- Scans only the **first** `<script>` block.
- Tracks brace, bracket and paren depth and quotes (`'`, `"`, `\` escapes).
- Handles `var a = …;` repeated on one line, `new Array(…)`, `0x` hex, unquoted keys, single-quoted strings and trailing commas.
- Converts each value to JSON text and parses it with `json.loads`. There is **no `json5` dependency**.
- Limits: 256 KB input, depth 8, 4,096 items per array, identifiers `[A-Za-z_]\w{0,63}`.
- Raises `ProtocolError` on violation. Pure; returns new objects.

**`tplink_easysmart_mcp/switch/pages.py`:**

| Function | Behaviour |
|---|---|
| `classify(html, anchor) -> PageClass` | — |
| `logon_err_type(html) -> int` | Raises `ProtocolError` if `logonInfo` is absent |
| `probe_login_page(html, headers) -> LoginProbe` | Returns `{session_model, auth_variant, login_mode, err_type}` |
| `ERR_TYPES` | Table of the 0–6 meanings |

**`tplink_easysmart_mcp/switch/models.py`.** Pydantic v2, frozen:

| Model | Fields |
|---|---|
| `SystemInfo` | name, mac, ip, netmask, gateway, firmware, hardware, `hw_revision` |
| `PortState` | number, enabled, `speed_config`, `speed_actual`, `link_up`, `fc_config`, `fc_actual`, `lag_id` |
| `PortStats` | number, enabled, link, `tx_good`, `tx_bad`, `rx_good`, `rx_bad` |
| `PoePort` | number, enabled, priority, `limit_kind` (auto, class1–4, manual), `limit_w`, `power_w`, `current_ma`, `voltage_v`, `pd_class` (`"0".."4"` or None), status, `raw` (the raw int tuple, used for RMW) |
| `PoeBudget` | — |
| `PoeSnapshot` | `poe_port_num`, budget, ports |
| `Vlan` | — |
| `VlanTable` | — |
| `PortPvid` | — |

Enums: `PortSpeed` (unknown codes are kept as `UNKNOWN` with `raw`), `PoePriority`, `PoeStatus` (0–9), `LoginMode`, `SessionModel`, `AuthVariant`.

**`tplink_easysmart_mcp/switch/parsers.py`.** Each parser has the form `parse_X(html) -> Model`: `parse_system_info`, `parse_ports`, `parse_port_stats`, `parse_poe`, `parse_vlans`, `parse_pvids`.
- Each **first** calls `classify`. A `LOGIN_PAGE` raises `SessionExpired`, never `ProtocolError` and never an empty result.
- Missing or short arrays raise `ProtocolError` naming the key and the lengths. There are no partial rows.
- A PoE `state` outside {0, 1} raises `ProtocolError`. That is an S0 confirm point.

**`tplink_easysmart_mcp/switch/forms.py`.** Pure builders; each returns `RequestPlan{method, path, fields: tuple[tuple[str,str],...]}`:
- `build_login_form(username, password, mode)`. **Raises `RestoredAccountMode` unless `mode == NORMAL`.**
- `build_poe_port_form(port: PoePort, enabled: bool)` re-sends the port's current priority and limit; only `name_pstate` changes.
- `build_port_setting_query(port: PortState, enabled: bool)` re-sends `spd_cfg` and `fc_cfg`.
- `plan_redacted(plan)` returns the plan with the password replaced by `"<redacted>"`. Dry-run and logging use it.

**`tplink_easysmart_mcp/switch/constants.py`:**
- paths;
- anchors;
- the write code maps;
- `PRESET_LIMIT2 = {2:"(4w)",3:"(7w)",4:"(15.4w)",5:"(30w)"}`;
- `AUTO_LIMIT2 = ""` (`# S0: confirm`);
- `PAD = 2` (`# S0: confirm`);
- `POE_STATE_DISABLED = 0` (`# S0: confirm`).

**`docs/protocol/easysmart-switch.md`** (from `switch-protocol.md` plus the S0 diff) and the **`NOTICE`** MIT credit.

**Tests** (`tests/`):
- **`test_jsvars.py`.** Edge and adversarial cases:
  - every fixture parses;
  - the hostile-description fixture yields the exact string, with `x` **not** leaked as a variable;
  - `;` inside strings, `\"` escapes, hex values, trailing commas, CRLF line endings, the BOM, an empty page, no `<script>`, an unterminated object;
  - a depth bomb (`{` × 1000), a 10,000-item array, a 1 MB page and NUL bytes are all rejected before parsing;
  - inputs are not mutated.
- **`test_pages.py`:**
  - `classify` on every fixture;
  - the probe returns `restored_account` for `login_page_restored_account.html`;
  - the probe returns `encrypted` for `login_page_encrypted_variant.html`;
  - the `submitForm` form name is **not** treated as an encrypted marker;
  - errType for each `logon_response_*`;
  - `logonInfo` missing raises `ProtocolError`;
  - `logonInfo` with a non-int raises `ProtocolError`.
- **`test_parsers_ports.py`:**
  - every `MANIFEST` expectation for `port_setting` and `port_statistics`;
  - padding of 18, and arrays shorter than 16 raising `ProtocolError`;
  - an unknown speed code 7 or 8 kept as `UNKNOWN`;
  - the login page raises `SessionExpired`.
- **`test_parsers_poe_vlan.py`:**
  - every `MANIFEST` expectation for PoE: the unpowered port 4, class `--` on ports 4 and 6, port 7 over budget, the manual 25.5 W limit, port 8 disabled, and the budget;
  - the truncated fixture raises `ProtocolError`;
  - `poe_config_returned_login_page.html` raises `SessionExpired`;
  - a negative or float power value raises `ProtocolError`;
  - VLAN bitmasks match the manifest;
  - `count` < `len(vids)` is respected;
  - when `state:0`, the result has `enabled=False`.
- **`test_forms.py`:**
  - RMW bodies equal the manifest `rmw_write_examples` exactly, including field order;
  - the priority map is +1;
  - each limit code;
  - a manual limit that equals a preset is rewritten as the preset (documented);
  - `build_login_form` with `RESTORED_ACCOUNT` or `UNKNOWN` raises;
  - `cpassword` is always `""`;
  - the password never appears in `plan_redacted`;
  - port 0, port 17 and a non-PoE port 9 are rejected by the builder's input model.

**Done when:**
- the gate passes;
- `tplink_easysmart_mcp/switch/` parsers have ≥ 90 % coverage;
- every `MANIFEST.json` expectation is asserted. A test iterates the manifest, so a fixture with no assertion fails.

## Phase S2 — client: session, breaker, serialised queue (one agent)

**Deliverables** (≤ 12 files):

**`tplink_easysmart_mcp/switch/errors.py`** defines `SwitchError` subclasses on top of `tplink_easysmart_mcp.core.errors`, and maps each to an envelope code:

| Code | Meaning |
|---|---|
| `AUTH_FAILED` | errType 1 |
| `LOCKED_OUT` | errType 2 |
| `SESSIONS_FULL` | errType 3 or 4 |
| `SESSION_TIMEOUT` | errType 5 |
| `RESTORED_ACCOUNT_MODE` | errType 6 |
| `AUTH_VARIANT_UNSUPPORTED` | — |
| `LOGIN_NOT_ACCEPTED` | — |
| `SESSION_BUSY` | — |
| `SESSION_LOST` | — |
| `LOGIN_COOLDOWN` | carries `retry_after_s` |
| `OUTCOME_UNKNOWN` | — |
| `PROTOCOL_ERROR` | — |

**`tplink_easysmart_mcp/switch/config.py`** implements the configuration table above, including the startup validation and the requirement that `PROTECTED_PORTS` is set when writes are allowed.

**`tplink_easysmart_mcp/switch/auth.py`.** `SwitchAuthenticator(settings, http, breaker, cooldown)`, with `probe()` and `login()`:

1. **Gate check, with no network.** Check `login_disabled`, then `breaker.check()`, then `cooldown.check()`.
2. **Probe:** `GET /`, then `probe_login_page`.
3. **Fail closed:**
   - `restored_account` → `breaker.record_failure` and raise;
   - `encrypted` or `unknown` → `breaker.record_failure` and raise;
   - `ip_bound_active` → raise `SESSION_BUSY` (no breaker).
4. **POST,** exactly once.
5. **Map errType:**
   - 1, 2, 6 or anything unknown → `breaker.record_failure(details)`, and **never retry**;
   - 3, 4 or 5 → `cooldown.record()`, then raise.
6. **Confirm** on errType 0 or a connection reset: `GET /SystemInfoRpm.htm`. If it returns `DATA`, the login succeeded. Otherwise `cooldown.record()` and raise `LOGIN_NOT_ACCEPTED`.

`logout()`:
- does nothing, and makes no network call, if not authenticated;
- otherwise `GET /Logout.htm`, then clears the jar;
- logs failures at WARNING and does not raise them.

The cooldown is `LoginCooldown(state_dir, device, seconds)`, a JSON file holding `last_failure_at`. It persists across restarts and is cleared on a confirmed login.

**`tplink_easysmart_mcp/switch/client.py`.** `SwitchClient` wraps the copied `core.transport.HttpTransport` (with a jar) and `core.serial.SerialExecutor`:
- `get_page(path, anchor) -> str`: serialised; lazy login; on `LOGIN_PAGE`, **exactly one** re-login and re-GET; a second `LOGIN_PAGE` raises `SESSION_LOST`.
- `submit(plan) -> None`: serialised; dry-run aware (it never sends when `DRY_RUN`; it returns the redacted plan); a connection reset raises `OUTCOME_UNKNOWN`.
- `typed readers`: `system_info()`, `ports()`, `port_stats()`, `poe()`, `vlans()`, each built on `get_page` and the parsers.
- `session_scope(logout_after: bool)`: an async context manager for mutating tools that always logs out in `finally`.
- No request body for `logon.cgi` is ever logged. Header and cookie values are redacted.

**`tplink_easysmart_mcp/switch/backend.py`.** `EasySmartSwitchBackend`. It implements the copied core's `DeviceBackend` protocol and is the only backend registered. `switch_status` calls its `healthcheck()`, which runs **only the probe**: no login and no POST. It returns `session_model`, `auth_variant`, `login_mode` and `breaker`/`cooldown` state.

**Tools:**
- `switch_check_auth()` runs the probe only.
- `switch_login()` makes one explicit login, then confirms, then logs out. It returns `session_model` and `hw/fw`. It **never** returns cookies.

**CLI** (`tplink_easysmart_mcp/cli.py`, from the copied core): `tplink-easysmart-mcp serve | --list-tools | check-auth [--login] | breaker --show|--clear`.

**Tests** (`respx`; no real device):
- **`test_auth.py`.** State machine and adversarial cases:
  - **The restored-account guard:** `GET /` returns `login_page_restored_account.html`. Assert **zero POSTs** to `/logon.cgi` (`respx` route `call_count == 0`), the breaker file is written, and `RESTORED_ACCOUNT_MODE` is raised.
  - The same guard when the POST response itself is errType 6: there is no further request of any kind.
  - The encrypted variant leads to zero POSTs.
  - errType 1 → exactly 1 POST and the breaker trips; the next `login()` makes **no network call**.
  - errType 2 → same as errType 1.
  - errType 4 → cooldown; a second `login()` within 300 s makes no network call and returns `LOGIN_COOLDOWN` with `retry_after_s`; after 300 s (frozen clock) the login is allowed.
  - **errType 0 + confirm returns the login page** → `LOGIN_NOT_ACCEPTED` and the cooldown is recorded (the issue #49 regression).
  - **errType 0 + confirm returns data** → success.
  - A connection reset on the POST → confirm, and no second POST.
  - `login_disabled` → no HTTP at all.
  - When `GET /` sets `H_P_SSID`, the POST carries it (cookie-model test).
  - When `GET /` returns the frameset with an empty jar → `SESSION_BUSY` and no POST.
  - The cooldown file persists across instances.
- **`test_client.py`:**
  - lazy login on the first read;
  - eviction mid-session (a data GET returns the login page) → one re-login, then success;
  - a second eviction in the same call → `SESSION_LOST`, with exactly 2 logins total;
  - concurrent `get_page` calls are serialised (assert there is no overlap with an `asyncio.Event` probe);
  - `session_scope` logs out even when the body raises;
  - logout when never logged in → no request;
  - dry-run `submit` → no request, and the plan is redacted;
  - a reset on `submit` → `OUTCOME_UNKNOWN`;
  - log capture contains no password and no cookie value.
- **`test_config.py`:**
  - password lengths 5 and 17 are rejected;
  - a password with spaces is rejected;
  - `ALLOW_WRITES=true` without `PROTECTED_PORTS` is a `ConfigError`;
  - `PORT_MAP` parsing: duplicate names, bad characters, port 0, an empty entry, a trailing `;`, and Unicode names are all rejected;
  - `VERIFY_TLS` set is rejected;
  - `POE_PORTS` accepts `"1-8"`, `"1,2,5-8"` and a reversed range is rejected, as are 0, junk and an empty value when writes are allowed.
- **`test_backend.py`:**
  - `switch_status` with the probe served by `respx` makes exactly one GET and no POST;
  - the redaction test (no password or cookie anywhere in the envelope);
  - a `POE_PORTS` / `poe_port_num` mismatch is reported.

**Done when:**
- the gate passes;
- `tplink-easysmart-mcp --list-tools` lists `switch_status`, `switch_check_auth` and `switch_login`;
- `tplink_easysmart_mcp/switch/` coverage is ≥ 85 %.

## Phase S3 — MCP tools (one agent; may split S3a reads / S3b writes if > 14 files)

Every tool:
- takes a pydantic input model;
- has an LLM-actionable docstring that says what it mutates and what it refuses;
- returns an `Envelope`;
- never raises.

`port` arguments accept an `int` or a `PORT_MAP` name. They resolve through `resolve_port(arg, snapshot)`:
- name lookup is case-insensitive;
- an unknown name returns `UNKNOWN_PORT`, with the known names listed;
- an out-of-range port returns `INVALID_PORT`.

**Read tools** (none of these log out unless `LOGOUT_AFTER_READS`):

| Tool | Returns |
|---|---|
| `switch_get_system_info` | model, `hw_revision`, firmware, MAC, IP, netmask, gateway, `session_model` |
| `switch_get_ports(only_linked=False)` | Rows of port, name (from the map), enabled, `link_up`, `speed_actual`, `speed_config`, flow control, `lag_id`, protected |
| `switch_get_port_stats(port=None)` | Counters for one port or all ports, plus `error_ports` (rx_bad + tx_bad > 0) |
| `switch_get_poe` | The budget, plus per-port rows with port, name, enabled, status, `power_w`, `current_ma`, `voltage_v`, `pd_class` (`null` → `"--"` in text), priority, limit, protected. Also derived lists: `unpowered_enabled_ports` and `fault_ports` (status ∈ 3, 4, 6–9). |
| `switch_get_vlans` | The 802.1Q table and per-port PVID. If the page is not a VLAN page (`UNEXPECTED`) or `qvlan_ds` is absent, return `NOT_SUPPORTED`. |

**Write tools.** Each one checks both gates **before any network call**, honours `DRY_RUN`, and runs inside `session_scope(logout_after=True)`:

**`switch_set_poe(port, enabled, confirm_write=False)`.**
- Refuses:
  - a non-PoE port (not in `EASYSMART_POE_PORTS`, or > the live `poe_port_num`) → `NOT_POE_PORT`;
  - a protected port → `PROTECTED_PORT`.
- If the port is already in the requested state: `changed:false` and no write.
- Otherwise:
  1. RMW `build_poe_port_form`;
  2. submit;
  3. re-read;
  4. verify `enabled` (and that `priority`/limit are unchanged; otherwise `CLOBBERED` with before/after).
- Returns `{before, after, changed}`.

**`switch_set_port(port, enabled, confirm_write=False)`.**
- Refuses a protected port.
- Otherwise RMW `build_port_setting_query`, then verify.
- Also refuses `enabled=False` on the **last** linked port that is not protected? **No.** Keep it simple: the protected list is the guard.

**`switch_poe_cycle(port_or_camera_name, off_seconds=10, confirm_write=False)`** (`cycle.py`).

Guards:
- `off_seconds` is clamped to the min/max bounds, and an out-of-range value is rejected (not silently clamped) with `INVALID_ARGUMENT`;
- a non-PoE port is refused;
- a protected port is refused;
- if PoE on the port is already off, return `ALREADY_OFF` and do not turn it on;
- **one cycle in flight** per backend, enforced with a non-blocking lock: a second call returns `CYCLE_IN_PROGRESS` immediately.

Steps:
1. Read, and capture the tuple.
2. Off: RMW + verify `enabled == False` within 10 s.
3. `asyncio.sleep(off_seconds)`.
4. On, with the **same captured tuple**, then verify `enabled`.
5. Poll at 2 s intervals for up to `CYCLE_POWER_TIMEOUT_S` until `status == on` and `power_w > 0`.
6. Log out.

Results:
- On success: `{port, name, off_at, on_at, powered_at, power_w, pd_class}`.
- If the on-step fails:
  - one fresh read;
  - if the port is still off, send exactly one more on;
  - otherwise, or if that also fails, return `CYCLE_INCOMPLETE` with the last state and the human text "port N (<name>) may be UNPOWERED".
- If power does not return in time: `POWER_NOT_RESTORED` (warning, `success:false`) with the PoE row. PoE *is* on, but the camera did not draw power.

Dry-run:
- do the read;
- return both planned bodies (off and on) and the sleep;
- send nothing;
- log out.

**Tests:**
- **`test_tools_read.py`:**
  - each tool against fixtures served by `respx`;
  - name resolution, including case and an unknown name;
  - the `--` class rendering;
  - `fault_ports` contains port 7;
  - `unpowered_enabled_ports` contains ports 4 and 6;
  - the VLAN page missing → `NOT_SUPPORTED`;
  - a login page mid-read → one re-login;
  - a malformed page → `PROTOCOL_ERROR` envelope (not an exception).
- **`test_tools_write.py`:**
  - writes disabled → `WRITE_NOT_ALLOWED` and **`respx` sees zero requests**;
  - `confirm_write=False` → same;
  - a protected port → refused, with zero writes;
  - port 9 → `NOT_POE_PORT`;
  - already in the requested state → no POST;
  - the RMW body equals the manifest examples;
  - after the write, a page showing changed priority → `CLOBBERED`;
  - a connection reset → re-read decides the outcome;
  - dry-run → exact redacted body and zero POSTs;
  - logout happens after success **and** after failure;
  - an injected name (`"1;sel_2=1"`, `"../"`, a 10 KB string, Unicode) → `UNKNOWN_PORT` and no request.
- **`test_cycle.py`** (state machine; frozen clock / fake sleep):
  - the happy path makes exactly 2 POSTs, with identical priority and limit fields;
  - a concurrent second cycle → `CYCLE_IN_PROGRESS` with no requests;
  - already off → `ALREADY_OFF`, with zero POSTs;
  - the on-POST times out and a re-read shows it off → exactly one retry, then success;
  - the on-POST fails twice → `CYCLE_INCOMPLETE` mentioning UNPOWERED;
  - the session is evicted during the sleep → one re-login before "on", and the cycle completes;
  - the breaker trips during the cycle (`AUTH_FAILED` on re-login) → `CYCLE_INCOMPLETE` with the port state unknown, and no further login;
  - power is not restored within the timeout → `POWER_NOT_RESTORED`;
  - `off_seconds` of 0, 4, 121, NaN, -1 or a string → `INVALID_ARGUMENT` with no network;
  - a camera name resolves through `PORT_MAP`.
- **README section** for the switch backend: tools table, write gating, the protected-ports requirement, the restored-account guard, the session-eviction warning ("the switch web UI will be logged out whenever the MCP acts, and vice versa"), and the cleartext-on-LAN deployment note.

**Done when:**
- the gate passes;
- `tplink-easysmart-mcp --list-tools` lists all 11 tools: `switch_status`, `switch_check_auth`, `switch_login`, and the 8 from this phase;
- `tplink_easysmart_mcp/switch/` coverage is ≥ 85 %.

## Phase S4 — LIVE verification (orchestrator-run, owner present)

**Preconditions:**
- S0 is done.
- The owner is logged out of the web UI.
- The camera port to cycle has been chosen: one non-critical camera.
- `EASYSMART_PROTECTED_PORTS` holds the uplink port and the server's port.
- `EASYSMART_PORT_MAP` is set.
- The NVR backend is working (for an independent check).

**Steps:**
1. `tplink-easysmart-mcp check-auth`: probe only. Expect `plain_form`, `normal`, and the S0 `session_model`.
2. `tplink-easysmart-mcp check-auth --login`: one login. Expect `hw/fw` to match S0.
3. Read tools: `switch_get_system_info`, `switch_get_ports`, `switch_get_poe`, `switch_get_vlans`, `switch_get_port_stats`. Compare them with the S0 captures and with what the owner sees physically (LEDs, which cameras are on).
4. Dry-run the cycle: `EASYSMART_DRY_RUN=true`, then `switch_poe_cycle("<camera>", 10, confirm_write=True)`. Read both bodies and check them against the S0 PoE-form findings, especially `AUTO_LIMIT2`.
5. Live cycle with `DRY_RUN=false` on the chosen camera.
   - Watch for `powered_at` and `power_w`.
   - **Confirm independently** that the NVR channel goes offline and comes back (`nvr_list_channels` on the separate `vigi-nvr-mcp` server, or the NVR UI).
   - If the S0 disabled-state value was "pending", it is now observed. Record it.
6. `switch_get_poe`: the port's priority and limit are unchanged from step 3.
7. Negative checks:
   - `switch_poe_cycle` on a protected port → `PROTECTED_PORT`, with no request in the logs;
   - port 9 → `NOT_POE_PORT`.
8. The owner logs into the web UI to confirm it still works and the port settings are intact. The MCP is idle while they do this.

Record the sanitised results as `docs/protocol/easysmart-switch-verified.md`, then delete `~/switch-capture/`.

## File and LOC budget

| Phase | New or changed files | Approx. LOC |
|---|---|---|
| S1 | jsvars, pages, models, parsers, forms, constants, docs, NOTICE, and 5 test files = 13 | ~1,300 |
| S0 | none (orchestrator) | — |
| S1 step 0 | Verbatim core copy plus scaffold. **Not counted**; it has its own commit. | — |
| S2 | errors, config, auth, client, backend, CLI wiring, and 4 test files (`test_auth`, `test_client`, `test_config`, `test_backend`) = 10 | ~1,200 |
| S3 | tools_read, tools_write, cycle, resolve (in tools_read), README, and 3 test files = 7–8 | ~1,100 |

Each phase stays within ≤ 14 files and ≤ 1,500 LOC (master plan §2). If S3 exceeds that, split it into S3a (reads) and S3b (writes and cycle).

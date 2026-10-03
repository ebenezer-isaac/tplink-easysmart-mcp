# TP-Link Easy Smart switch protocol (TL-SG1016PE) — implementation notes

Authoritative source: `docs/specs/switch-protocol.md` and
`docs/specs/switch-endpoints.json`. This file records how phase S1 implements
those facts and lists what **S0 (the live capture)** must still confirm. S0's
findings override this document.

Credit: the request and parsing logic is ported from
`vmakeev/hass_tplink_easy_smart` (MIT, © 2022 Vladimir Makeev). No files copied.
See `NOTICE`.

## Transport (not exercised in S1 — pure parsing only)

- Plain HTTP on `:80`. HTTP 200 says nothing about auth: an unauthenticated
  request to any page returns the login page with status 200.
- Classify the body, never the status or size (`pages.classify`).
- Phases S2+ send `Referer: http://<host>/`, use a 5 s timeout, and keep one
  request in flight.

## Body classification (`pages.py`)

Classification is **structural**, not a substring scan. The switch speaks
cleartext HTTP on the LAN and reflects user-settable strings (VLAN names, the
device description, both ≤ 16 chars) into data pages, so a body substring like
`var logonInfo` or `action="/logon.cgi"` is attacker-controllable and must never
by itself decide the class.

| Class | Rule |
|---|---|
| `LOGIN_PAGE` | the first `<script>` block declares `var logonInfo = [...]` as a top-level statement (read with the `jsvars` tokenizer), **and/or** a `<form>` element's parsed `action` attribute equals `/logon.cgi` (read with stdlib `html.parser`) |
| `DATA` | the body declares the page's anchor variable (`var <anchor>`) |
| `UNEXPECTED` | anything else (the parsers raise `ProtocolError`) |

Anchors: `info_ds`, `all_info`, `portConfig`, `qvlan_ds`, `pvid_ds`.

Why structural: `html.parser` treats `<script>`/`<style>` bodies as CDATA, so a
marker that appears only inside a quoted script string, an HTML comment, or a
reflected attribute never reaches `handle_starttag` and cannot flip the class. A
VLAN named `var logonInfo`, or one carrying `action="/logon.cgi"` as text, stays
`DATA` (breaker r1 F1).

## Login classification (`pages.probe_login_page`)

- `err_type = logonInfo[0]` (0–6). `logon_err_type` raises `ProtocolError` when
  `logonInfo` is absent or non-integer.
- `auth_variant`: `encrypted` if any of `encryptType`, `cryp_new`,
  `plain_password`, `g_tid`, `securityEncode` appears **as a token in a `<script>`
  block body or a `<script src>` attribute**; else `plain_form` for a login page;
  else `unknown`. The form name `submitForm` is **not** a marker, and a marker
  that appears only in an HTML comment or ordinary text does not count (breaker r1
  F3).
- `login_mode`: `restored_account` iff `err_type == 6` (authoritative), **or** the
  form carries a *visible* new-password field (an `<input type="password">` whose
  name is `cpassword`/confirm/new and is not inside a `display:none` element). A
  hidden static `value="Confirm"` input — present in the stock template and
  revealed by JS only when `errType == 6` — is **not** a signal (breaker r1 F4).
  The dangerous direction stays fail-closed: a genuine restored page has
  `logonInfo[0] == 6`, so the builder never silently POSTs into restored mode.
- `session_model`: `cookie` if `Set-Cookie: H_P_SSID` is present; else `ip_bound`
  for a login page; else `ip_bound_active` (another process on our IP is logged
  in — do not log in).

### Restored-account hard guard

`forms.build_login_form` raises `RestoredAccountMode` for any mode other than
`NORMAL`, and `cpassword` is always empty. In factory-reset mode a POST to
`/logon.cgi` would **set** the admin password, so no code path may build a login
body there. (Enforcement in the auth/client flow is added in S2.)

## errType table

| errType | Meaning | S2 handling |
|---|---|---|
| 0 | OK **or** silently ignored (byte-identical) | verify with a data GET |
| 1 | bad credentials | `AUTH_FAILED`, trip breaker, never retry |
| 2 | user not allowed | `LOCKED_OUT`, trip breaker |
| 3, 4 | user/session slots full | `SESSIONS_FULL`, 5-min cooldown |
| 5 | session timeout | cooldown |
| 6 | restored-account mode | `RESTORED_ACCOUNT_MODE`, never POST |

## Read-page encodings (`parsers.py`, `constants.py`)

- Power, voltage, power-limit and the global budget are **tenths** (÷10).
- Priority reads 0/1/2 = high/middle/low; the write encoding is **+1**.
- `powerlimit` 330 = auto; 40/70/154/300 = class 1–4 presets; anything else is a
  manual limit in tenths of a watt.
- `pdclass` 40/70/154/300/330 → classes 1/2/3/4/0; anything else → `null` ("--").
- `powerstatus` 0–9 (off…over-temperature).
- Per-port arrays on `PortSettingRpm`/`PortStatisticsRpm` are `max_port_num + 2`
  long; `pkts` is `tx_good, tx_bad, rx_good, rx_bad` per port plus 2 pad slots.
  Parsers use the first `max_port_num` entries and raise `ProtocolError` if fewer
  are present.

## Write forms (`forms.py`)

- **PoE** `POST /poe_port_config.cgi`: `sel_<n>=1`, `name_pstate` (2 on / 1 off),
  `name_ppriority` (read+1), `name_ppowerlimit` (1 auto … 6 manual),
  `name_ppowerlimit2` (preset literal / manual watts / `AUTO_LIMIT2`), `applay`
  (the misspelling is the real field name). One port per request.
- **Port** `GET /port_setting.cgi?portid&state&speed&flowcontrol&apply` —
  re-sends the current `spd_cfg`/`fc_cfg`.
- A manual limit that equals a preset (e.g. 15.4 W) reads back as that preset and
  is therefore re-sent as the preset. The effective limit is identical.

## S0 — still to confirm (fact table in `constants.py`, tagged `# S0: confirm`)

- `AUTO_LIMIT2` — the exact `name_ppowerlimit2` bytes a browser sends for auto
  (default `""`; may become `"None"` or an omitted field).
- `PAD = 2` — the `max_port_num + 2` array padding on 16-port pages.
- `POE_STATE_DISABLED = 0` — the read value of a disabled PoE port's `state`.
- Whether `Logout.htm` ends the session, the session model (cookie vs IP-bound),
  and whether the 802.1Q pages exist under these names on this firmware.

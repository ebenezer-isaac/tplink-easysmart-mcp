# TP-Link Easy Smart web protocol (TL-SG1016PE V1/V3 family)

This is a read-only study. No device was contacted for it. The machine-readable form is `switch-endpoints.json`, and the parser fixtures are in `fixtures/` (with expectations in `fixtures/MANIFEST.json`).

**Confidence tags**
- **VERIFIED** means at least one of these holds:
  - The reference client's code does it.
  - The owner's own fingerprinted login page shows it.
  - Two independent clients agree on it.
- **INFERRED** means one of these holds:
  - It was seen only on sibling models.
  - It was deduced.
  - It is a guess that S0 must confirm.

**Sources**

| Tag | Source | License | How it was used |
|---|---|---|---|
| **[REF]** | `vmakeev/hass_tplink_easy_smart` v0.3.2.2, commit `857d27a` (2026-02-10), `custom_components/tplink_easy_smart/client/` | MIT | Logic is ported; the code is not vendored. Credit it in the repo `NOTICE` and module docstrings. It lists TL-SG1016PE V1 and V3 as fully supported, including PoE. |
| **[REF#n]** | Issues on [REF]. #49: login silently ignored without a prior GET, and errType 0 false positive. #7, #2, #50: browser and integration evict each other. Two of these reports are from **TL-SG1016PE** owners. | — | Behaviour evidence |
| **[FP]** | The owner's switch login page, fetched unauthenticated: `router/js/tl-sg1016pe/index.html`, 15,320 B | — | Primary evidence for the login page |
| **[QWE]** | `qwe7002-ai/tplink-easy-smart-switch-mcp`, with real page captures from a TL-SE2106 (fw 1.8.1 Build 20251128) | No license stated | Read only, for names, layouts and the encrypted-login variant |
| **[C6U]** | `tplinkrouterc6u`'s `TPLinkSG108EClient` | GPL-3.0 | Page and variable names only. No code was taken. |
| **[TLSG]** | `0ghny/tlsg10x` | MIT, Go, SG108E | Reference |
| **[SD]** | `t0mer/SwitchDeck` `tplink` package, SG108E | Apache-2.0 | Read in the `tools/sd_*.go` copies |
| **[AF]** | `router/tl-sg1016pe-auth-flow.md`, a byte-offset analysis of **[FP]**. This doc agrees with it. | — | Cross-check |

The endpoint map `switch-endpoints.json` is the **single merged file**. It supersedes `router/tl-sg1016pe-endpoints.json` and records the corrections in `_reconciliation`. The main correction: the old file listed the *write* codes as the PoE *read* encodings.

## 1. Transport

- The switch serves plain HTTP on :80 only. There is no TLS. Ports 22, 23 and 443 are closed **[FP]**.
- Responses are minimal: `HTTP/1.1 200`, `Connection: close`, `Content-Type: text/html`, and no `Server` header **[FP]**.
- **HTTP status is meaningless for auth.** An unauthenticated or expired request to any page still returns `200` with the login page. Classify the body instead (§3.4).
- Send `Referer: http://<host>/` on every request, as a browser does. **[REF#8]** says logon needed it on a hw 5.2 unit (INFERRED as a requirement, but harmless).
- Form posts use `application/x-www-form-urlencoded`. Writes use GET query strings or POST bodies, depending on the handler (§6).
- **Connection resets.** Some SG108E builds reset the TCP connection instead of answering a POST (login, writes, reboot) **[SD]**. Treat a reset after a mutating request as **outcome unknown**, never as success, and resolve it by re-reading the page. Never resend the request blindly.
- Keep timeouts short (5 s, as in **[REF]**). The web server is single-threaded and fragile.

## 2. Inline variable extraction

Every `*Rpm.htm` page begins with one `<script>` block of `var NAME = VALUE;` statements, then the UI HTML. These are the value forms seen:

| Form | Example | Pages |
|---|---|---|
| Integer | `var max_port_num = 16;` | most |
| String | `var tip = "";` | all |
| JS object literal with unquoted keys, arrays, `'single'` strings and `0x` hex | `var all_info = {\nstate:[1,1,…,0,0],\n…\n};` | most |
| `new Array(...)` | `var logonInfo = new Array(\n0,\n0,0);` | login page |
| Statements on one line separated by `;` | `};var tip = "";` | VLAN pages **[QWE]** |

**How [REF] parses (VERIFIED):**
- It runs the regex `var\s+(\w+)\s*=\s*([^;]+);` over the page.
- Objects go through `json5.loads`.
- Arrays are split on `,` and stripped of quotes.

**Weakness:** a `;` inside a string value (for example in the switch description) truncates the value, and the parse fails. Fixture `system_info_hostile_description.html` proves this.

**Our extractor should be a pure, brace- and quote-aware scanner.** It must:
- Find `var <ident> =` only at statement start in the **first** `<script>` block.
- Read the value by tracking `{}[]()` depth and `'`/`"` strings with `\` escapes, up to the terminating `;` or newline at depth 0.
- Convert the value: hex to int, unquoted keys to quoted, single-quoted strings to double-quoted, trailing commas dropped. Then parse it with `json.loads`. This needs no `json5` dependency and handles `0x`.
- Cap input at 256 KB per page, nesting depth at 8, and array length at 4,096. Return a new `dict[str, JsValue]` and never mutate.

**Array padding.**
- The per-port arrays on `PortSettingRpm` and `PortStatisticsRpm` have `max_port_num + 2` entries (8 → 10 on SG108E **[TLSG]**, 6 → 8 on SE2106 **[QWE]**). For 16 ports this is INFERRED to be 18.
- Parsers take the first `max_port_num` entries and raise `ProtocolError` if fewer are present.
- PoE arrays are assumed to be `poe_port_num` long (INFERRED). Index them the same way.

## 3. Authentication and session

### 3.1 Login page: `GET /` (VERIFIED [FP])

```
<script>
var logonInfo = new Array(
0,
0,0);
var g_Lan = 1;
var g_year=2022;
</script>
… <FORM id="form0" method="POST" name="submitForm" action="/logon.cgi">
  username (text, maxlength 16) · password (password, maxlength 16)
  cpassword (password, row hidden unless errType 6) · submit name="logon" value="Login"
```

- The client-side check `doOnclick()` only validates two things:
  - the username: 1–16 chars, `/^[\x21-\x7e]+$/`;
  - the password: **6–16 chars** (`t_error10`), no spaces.

  **There is no hashing or encryption: the password crosses the LAN in cleartext.**
- Our config validator enforces the same bounds at startup. That way a bad password never reaches the device, where it would trip the breaker.
- `onload` does the following:

  ```
  if (errType != 0 || url does not contain "logon.cgi"/"lupg_abort.cgi")
      show the form
  else
      document.location.href = "/"
  ```

  That is, **a successful POST to `/logon.cgi` returns this same login page with errType 0, and the JS then navigates to `/`.**

### 3.2 `errType` = `logonInfo[0]` (VERIFIED, from [FP] JS and [REF])

| errType | UI string | Meaning | Client action |
|---|---|---|---|
| 0 | none | OK **or** login silently ignored. These are byte-identical (§3.3). | Verify with one data GET |
| 1 | "The user name or the password is wrong." | Bad credentials | `AuthFailed`. **Trip the breaker. Never retry.** |
| 2 | "The user is not allowed to login." | User blocked or locked out (duration unknown) | `LockedOut`. **Trip the breaker. Never retry.** |
| 3 | "The number of the user that allowed to login has been full." | No free user slot | `SESSIONS_FULL`. Rate-limit to 1 login per 5 min. No retry. |
| 4 | "…it is allowed 16 people to login at the same time." | Session table full | Same as 3 |
| 5 | "The session is timeout. Please login again." | Stale session | `TokenExpired`-like. No auto retry inside the same call. |
| 6 | "To protect your network… please change your password." | **Restored-account (factory-reset) mode.** See below. | `RESTORED_ACCOUNT_MODE`. Trip the breaker. **No POST, ever.** |
| other | "Unkonwn error" (sic) | — | `ProtocolError`. Trip the breaker. |

**Restored-account hard guard (VERIFIED [FP] @10015, [AF] §1).**
- When `errType == 6` the page's JS changes the form:
  - it hides the username;
  - it relabels the password "New Password:";
  - it shows "Confirm Password" (`cpassword`);
  - it changes the submit value to `Confirm`;
  - it sets `account_restored=1`.
- In this mode a POST to `/logon.cgi` **sets the initial admin password**. It does not authenticate.
- The client must therefore:
  1. Classify the `GET /` probe body. If `logonInfo[0] == 6`, or the body contains `account_restored=1` or `value="Confirm"`, raise `RESTORED_ACCOUNT_MODE` **before** building any POST, and trip the breaker.
  2. Make the login form builder take the probe's `LoginMode` as a required argument, and make it raise for any mode other than `normal`. That way no code path can post into restored mode.
  3. Never send a non-empty `cpassword`.
  4. If a logon response itself returns errType 6, stop and send nothing more.
- The S1 and S2 tests assert that zero POSTs are made (fixture `login_page_restored_account.html`).

**`logonInfo[1]` and `logonInfo[2]`** are unexplained; they are always `0` in every sample. Log them, but do not interpret them.
- The page states no attempt counter or lockout duration, so assume the conservative case.

### 3.3 Login request and the errType-0 ambiguity

```
POST /logon.cgi
Referer: http://<host>/
Content-Type: application/x-www-form-urlencoded

username=<u>&password=<p>&cpassword=&logon=Login
```

- **Fields** (VERIFIED): `username`, `password`, `logon=Login` are from **[REF]**, **[TLSG]**, **[SD]** and **[FP]**. `cpassword=` (empty) is sent by **[C6U]**, and the browser also sends it because the input is inside the form.
- **[REF] does not do `GET /` first, and its `_refresh_session()` clears cookies before the POST.** On cookie-variant firmware the POST is then silently ignored, and the reply is the pristine login page with errType 0, which **[REF]** reports as success. This was proven by bisection in **[REF#49]**: only the pre-login GET mattered, not headers, timing or the client.
- **Required sequence (for both session models):**
  1. Clear the jar, then `GET /`, and store any `Set-Cookie`.
  2. Run the auth_variant probe (§3.6) and the login-mode probe (restored-account guard, §3.2) on that body. Stop there if either fails.
  3. `POST /logon.cgi` (exactly one).
  4. If errType ≠ 0, map it per §3.2 and stop.
  5. If errType = 0, **verify** with `GET /SystemInfoRpm.htm`. Real data (contains `info_ds`, no `logonInfo`) means the session is established. A login page means `LOGIN_NOT_ACCEPTED` (ProtocolError). Count that as a login failure for the 5-minute rate limit, but do **not** trip the human-clear breaker, because the credentials may be fine.
  6. A connection reset on the POST is handled like step 5. Verify, and never re-POST.

### 3.4 Classifying any response body (pure function)

| Class | Rule |
|---|---|
| `LOGIN_PAGE` | Contains `var logonInfo` **or** `action="/logon.cgi"`. Size about 15,320 B on the owner's firmware, but never key on size. |
| `DATA` | Contains the page's expected anchor variable: `info_ds`, `all_info`, `portConfig`, `qvlan_ds`, `pvid_ds`. |
| `UNEXPECTED` | Anything else, including an empty body. Raise `ProtocolError` with the first 120 chars, after redaction. |

### 3.5 Session models: describe both, then detect which one applies

**A. IP-bound** (classic Easy Smart; CVE-2017-17746 class)
- After a good login, the switch marks the **client IP** as authenticated. There is no cookie and no token.
- Any process on that IP, for example the owner's browser on the same host, shares the session, and the reverse is also true.
- The owner's switch set **no cookie** on `GET /` **[FP]**, which points to this model (INFERRED until S0).

**B. Cookie** (`H_P_SSID`)
- `GET /` (any request) returns `Set-Cookie: H_P_SSID=tplink_<prefix><switch-mac><client-ip-hex><suffix>; Max-Age=600` **[REF#49]**, seen on TL-SG1428PE v3.
- The session ID encodes the owner IP. `logon.cgi` only binds the session if the request carries that cookie.
- The idle lifetime is 600 s.

**Detection probe** (no credentials; part of `check-auth` and S0):
1. Use a fresh jar and send `GET /`.
2. `Set-Cookie` with name `H_P_SSID` means `session_model = "cookie"`. No cookie means `"ip_bound"`.
3. If the body is **not** a login page while the jar is empty, then `"ip_bound_active"`: some process on our IP is already logged in.
   - Do not log in.
   - Report this, and in S0 make the owner log out of the browser first.

Both models use the same client code: always `GET /` first, keep a jar (it is a no-op when no cookie is set), log in, verify. Store `session_model` in the healthcheck output. Never put cookie values there; they are redacted by key `cookie`.

**Eviction and concurrency** (VERIFIED behaviour on TL-SG1016PE, from [REF#7] and [REF#2]):
- The login page claims 16 concurrent users (`t_error4`), but in practice **there is one effective session**.
- An MCP login kicks the owner's browser back to the login page, and a browser login kicks the MCP.
- So the client must:
  - Log in lazily.
  - Re-login **at most once** when a data GET returns `LOGIN_PAGE`.
  - Log out after every mutating tool.
  - Keep calls short and serialised.

**Idle timeout:**
- Cookie model: 600 s (VERIFIED on SG1428PE).
- IP-bound model: unknown (`t_error5` exists). Assume ≤ 10 min.
- Do not keep the session alive with background traffic.

### 3.6 `auth_variant` probe (pure function on the `GET /` body)

| Variant | Markers | Support |
|---|---|---|
| `plain_form` | Form `action="/logon.cgi"` with `name="password"`, plus none of the markers below | Supported (this is the owner's switch today **[FP]**) |
| `encrypted` | Any of: `var encryptType`, `cryp_new.js`, `plain_password`, `g_tid` (token), `securityEncode`, `RSA`/`rsa` key vars | **Unsupported.** Raise `AUTH_VARIANT_UNSUPPORTED` before any POST. **[QWE]** shows a salted XOR-table password encoding plus a `top.g_tid` token on every cgi; port that later if a firmware update brings it. |
| `unknown` | No `logonInfo` and no logon form | `ProtocolError`. Do not POST. |

**Do not** treat `name="submitForm"` as an encrypted-variant marker. The owner's plain page uses that form name (**[QWE]**'s heuristic would misfire here).

### 3.7 Logout

- `GET /Logout.htm` returns the login page and ends the session for this client. It is VERIFIED on SG108E (**[TLSG]**, **[SD]**) and on the TL-SE2106 menu (`location.href="/Logout.htm"`, **[QWE]**). For TL-SG1016PE it is INFERRED, and S0 confirms it.
- **[FP]** saw `/logout.cgi` also return the login page while unauthenticated, but that proves nothing about it.
- After logout, clear the jar. Logout failures are logged, not raised. The tool result still reports the write outcome.

## 4. Read pages

### 4.1 `GET /SystemInfoRpm.htm`: system info (VERIFIED)

```js
var info_ds = {
descriStr:["TL-SG1016PE"], macStr:["00:00:5E:00:53:10"], ipStr:["192.0.2.10"],
netmaskStr:["255.255.255.0"], gatewayStr:["192.0.2.1"],
firmwareStr:["1.0.0 Build 20000101 Rel.00000"], hardwareStr:["TL-SG1016PE 3.0"]
};
```

- Each value is a one-element array. **[REF]** returns `None` unless the length is exactly 1, and we do the same: a field is null otherwise.
- `hardwareStr` = `"<model> <rev>"`. Split on the last space to get `hw_revision` (`"3.0"`).
- Newer builds add `dhcpEnable`, `dnsStr` and `workTime` **[QWE]**. Accept them as optional extras and ignore unknown keys.

### 4.2 `GET /PortSettingRpm.htm`: per-port config and link (VERIFIED)

```js
var max_port_num = 16;
var port_middle_num  = 8;
var all_info = {
state:[...18], trunk_info:[...18], spd_cfg:[...18], spd_act:[...18], fc_cfg:[...18], fc_act:[...18]
};
```

**Fields:**
- `state`: 1 = enabled, 0 = disabled.
- `trunk_info`: LAG id, or 0.
- `fc_cfg` / `fc_act`: 0 or 1.
- Speed enum for `spd_cfg` / `spd_act` (**[REF]** `PortSpeed`):

  | Code | Meaning |
  |---|---|
  | 0 | link down |
  | 1 | auto (config only) |
  | 2 | 10M half |
  | 3 | 10M full |
  | 4 | 100M half |
  | 5 | 100M full |
  | 6 | 1000M full |
  | ≥ 7 | `unknown` (keep the raw code; SE2106 shows 7 and 8) |

- `link_up = spd_act != 0`.

### 4.3 `GET /PortStatisticsRpm.htm`: counters

- VERIFIED on SG108E by two clients; INFERRED for TL-SG1016PE.
- Variables: `max_port_num`, and `all_info = {state:[…], link_status:[…speed enum…], pkts:[…]}`.
- `pkts` is laid out as `[tx_good, tx_bad, rx_good, rx_bad]` × port, plus 2 pad entries.
- There are no byte counters.

### 4.4 `GET /PoeConfigRpm.htm`: PoE (VERIFIED names, scales and enums from [REF]; working on TL-SG1016PE 3.0 per [REF#2])

```js
var poe_port_num = 8;
var portConfig = {
state:[…], priority:[…], powerlimit:[…], power:[…], current:[…], voltage:[…], pdclass:[…], powerstatus:[…]
};
var globalConfig = {
system_power_limit:…, system_power_consumption:…, system_power_remain:…,
system_power_limit_min:…, system_power_limit_max:…
};
```

**`portConfig`**

| Key | Raw | Meaning |
|---|---|---|
| `state` | 1 / 0 | PoE enabled. **[REF]**: `enabled = state == 1`. The disabled value is INFERRED to be 0, and the parser rejects any other value. |
| `priority` | 0 / 1 / 2 | high / middle / low. **The write encoding is +1.** |
| `powerlimit` | ×10 W | 330 = **auto**; 40 / 70 / 154 / 300 = **class 1–4 preset** (4 / 7 / 15.4 / 30 W); anything else = **manual**, value / 10 W (range 0.1–30.0) |
| `power` | ×10 W | live draw |
| `current` | mA | live |
| `voltage` | ×10 V | live |
| `pdclass` | 40 / 70 / 154 / 300 / 330 | class 1 / 2 / 3 / 4 / **0**. Anything else is **"--"** (no or undetermined class; we emit `null`). |
| `powerstatus` | 0–9 | See the table below |

`powerstatus` values:

| Code | Status |
|---|---|
| 0 | off |
| 1 | turning on |
| 2 | on |
| 3 | overload |
| 4 | short |
| 5 | non-standard PD |
| 6 | voltage high |
| 7 | voltage low |
| 8 | hardware fault |
| 9 | over-temperature |

**`globalConfig`** values are all ×10 W. The "over budget" condition is derived: when a port is enabled with a class-4 PD, its `powerstatus` is 3 (overload) or 0 while `system_power_remain` < the class power.

**Ambiguity:** a manual limit of exactly 4.0, 7.0, 15.4, 30.0 or 33.0 W reads back as the matching preset or auto code. Read-modify-write therefore rewrites it as that preset. The effective limit is identical, so this is acceptable, but document it.

**TL-SG1016PE V1/V3 hardware:** 8 PoE+ ports (1–8). Do not hardcode the budget; read `globalConfig`.

### 4.5 802.1Q VLAN (INFERRED for this model; layout VERIFIED on TL-SE2106 [QWE] and in SwitchDeck's SG108E parser)

**`GET /Vlan8021QRpm.htm`**

```js
var qvlan_ds = {
state:1, portNum:16, vids:[1,10,20], count:3, maxVids:32,
names:['Default','cams','mgmt'],
tagMbrs:[0x0,0x8000,0x8000], untagMbrs:[0xff00,0xff,0x0],
lagIds:[…16…], lagMbrs:[0,0x0]
};var tip = "";
```

- Port `p` corresponds to bit `p-1`.
- Use only the first `count` entries of `vids` and `names`.
- Classic firmware may lack `lagIds`/`lagMbrs`, so treat them as optional.

**`GET /Vlan8021QPvidRpm.htm`**

```js
var pvid_ds = {state, portNum, vids, count, mbrs:[bitmask per vid], pvids:[per port], lagIds, lagMbrs};
```

**If 802.1Q is disabled:** `state:0`. Return `enabled:false` along with whatever table is present.

**If the page is missing on this firmware:** S0 lists alternatives from the menu. Candidates are `VlanPortBasicRpm.htm` (`pvlan_ds`) and `VlanMtuRpm.htm`. `switch_get_vlans` returns `NOT_SUPPORTED` rather than guessing.

## 5. Session-independent facts

- The hw revision and firmware come only from `SystemInfoRpm.htm`, which requires login, or from the UDP utility protocol, which is **out of scope**: broadcast, RC4 with a hardcoded key, credential-leaking (CVE-2017-8074 to 8077).
- `httpupg.cgi` firmware upload, pre-auth per CVE-2017-8078, is never called.

## 6. Write endpoints

All writes do the following:
1. Read the page.
2. Build the full request from current state plus one change. This step is a pure function, so dry-run can show the exact bytes.
3. Send the request once.
4. Re-read the page and compare (`verify`).
5. Log out.

### 6.1 `GET /port_setting.cgi`: port admin state (VERIFIED [REF], [SD])

```
/port_setting.cgi?portid=<1..16>&state=<1|0>&speed=<spd_cfg current>&flowcontrol=<fc_cfg current>&apply=Apply
```

- One port per call.
- `speed` and `flowcontrol` **must be re-sent from the current `spd_cfg`/`fc_cfg`**. Otherwise the call changes them.
- **[SD]** notes that some SG108E UIs mark the form `multipart/form-data`, but every client that works uses the GET query. Use GET.
- Refuse ports in `EASYSMART_PROTECTED_PORTS`. Disabling the uplink or the server's own port locks the server out of the switch.

### 6.2 `POST /poe_port_config.cgi`: per-port PoE (VERIFIED [REF])

```
sel_<n>=1&name_pstate=<2 on|1 off>&name_ppriority=<1|2|3>&name_ppowerlimit=<1..6>&name_ppowerlimit2=<see 6.3>&applay=Apply
```

| Field | Values |
|---|---|
| `sel_<n>` | One `sel_` per selected port. All selected ports get the *same* values, **so always select exactly one**. |
| `name_pstate` | **2 = enable, 1 = disable.** This differs from the read encoding (1 = on). |
| `name_ppriority` | read value + 1 (high 1, middle 2, low 3) |
| `name_ppowerlimit` | 1 auto · 2 class1 · 3 class2 · 4 class3 · 5 class4 · 6 manual |
| `name_ppowerlimit2` | manual: watts `"25.5"` (0.1–30.0, one decimal) · class presets: `"(4w)"`, `"(7w)"`, `"(15.4w)"`, `"(30w)"` (verbatim from **[REF]**) · auto: §6.3 |
| `applay` | `Apply`. **The misspelling is the real field name.** |

**Read-modify-write is mandatory.** The handler applies every field, so a `poe_set(port, enabled)` that guesses priority or limit silently resets them. Build the request from the port's current `(priority, powerlimit)` and change only `name_pstate`.

### 6.3 Open point: `name_ppowerlimit2` for auto (INFERRED)

- **[REF]** maps `AUTO → (1, None)` and posts a dict through aiohttp. aiohttp url-encodes non-string values with `str()`, so the wire value is most likely the literal `None`. This is INFERRED from aiohttp semantics, and it is known to work on TL-SG1016PE 3.0.
- Browsers send whatever the page's inline submit JS produces. This is most likely an empty or omitted field, because the text box is disabled unless "Manual" is chosen.
- **S0 resolves it** by reading the `<form>` and submit JS in the captured `PoeConfigRpm.htm` (all inline). It then records the browser's exact field list per limit type in `docs/protocol/easysmart-switch.md`.
- Until then, the builder takes `AUTO_LIMIT2` from one constant. The default is `""`, and S0 may change it to `"None"` or "omit". Fixture tests assert against the constant, not a literal.

### 6.4 `POST /poe_global_config.cgi`: budget (VERIFIED [REF], not exposed)

`name_powerlimit=<W>&name_powerconsumption=<current W>&name_powerremain=<current W>&applay=Apply`. `name_powerlimit` must lie within `system_power_limit_min..max` / 10.

### 6.5 VLAN writes (INFERRED, not exposed)

There are two incompatible shapes. **[SD]** (SG108E) POSTs the whole table to `/Vlan8021QRpm.htm` (`state, count, vid<i>, name<i>, tagMbr<i>, untagMbr<i>, apply=apply`). **[QWE]** (TL-SE2106) uses one GET to `qvlanSet.cgi` per VLAN, as follows:

- `GET /qvlanSet.cgi?qvlan_en=1|0&qvlan_mode=Apply`
- `GET /qvlanSet.cgi?vid=..&vname=..&selType_<p>=0|1|2(all ports)&qvlan_add=Add/Modify`
- `GET /qvlanSet.cgi?selVlans=<vid>&qvlan_del=Delete`
- `GET /vlanPvidSet.cgi?pbm=<mask>&pvid=<vid>`
- On encrypted-variant firmware these also take `&token=<top.g_tid>`.

### 6.6 Reboot and save (INFERRED, not exposed)

- **Reboot.** The candidates disagree:
  - `POST /reboot.cgi` with `reboot_op=reboot&save_op=1&apply=Reboot` (names from **[C6U]**).
  - `reboot_op=reboot&save_op=false` (**[SD]**).
  - `POST /SystemRebootRpm.htm` with `reboot=reboot` (**[SD]**).

  S0 captures `SystemRebootRpm.htm` to settle it. The connection resets as the switch goes down.
- **Save config.** `POST /savingconfig.cgi action_op=save` exists on the newer TL-SE UI (`SavingConfigRpm.htm`, **[QWE]**). It is unknown whether classic TL-SG1016PE persists each change immediately or needs a save; the `save_op` flag on reboot hints at a running/startup split.
  - S0 checks the menu for a save entry.
  - The tools never save. A PoE power cycle restores the original state anyway, so persistence only matters for `switch_set_poe(off)` across a switch reboot. The tool docstring must state the S0 finding.

### 6.7 Other pages (INFERRED, read-only candidates, not in v1 tools)

| Page | Variable or endpoint | Source |
|---|---|---|
| `IpSettingRpm.htm` | `ip_ds` | **[C6U]** |
| `PortTrunkRpm.htm` | LAG | — |
| `PortMirrorRpm.htm` | — | — |
| `IgmpSnoopingRpm.htm` | `igmp_ds` | — |
| `LoopPreventionRpm.htm` | `lpEn` | — |
| QoS pages | — | **[SD]** |
| `TurnOnLEDRpm.htm` | `led` | **[C6U]** |
| Cable diagnostics | Page and handler are **UNKNOWN**. No library implements it. | S0 captures the menu entry. |

See `switch-endpoints.json`.

## 7. Power cycle (composed; there is no native per-port PoE reboot)

1. Read PoE.
2. Assert that the port is PoE-capable (≤ `poe_port_num`), not protected, and currently `state == 1`. If PoE is already disabled, refuse with `ALREADY_OFF` and do not turn it on.
3. RMW write with `name_pstate=1`.
4. Re-read until `state == 0` and `power == 0` (timeout 10 s).
5. Sleep `off_seconds`, clamped to 5–120.
6. RMW write with `name_pstate=2`, re-sending the same priority and limit captured in step 1.
7. Re-read until `state == 1`.
8. Poll up to 60 s for `powerstatus == 2` (on) and `power > 0`, and report the PD class and watts.
9. Log out.

**If step 6 fails**, run `on` once more, but only after a fresh read shows the port still off. Then surface `CYCLE_INCOMPLETE` with the last-read state, so a human knows the camera is unpowered.

## 8. Wire-level examples (dummy values)

```
GET  /                      -> 200 login page (errType 0) [+ Set-Cookie: H_P_SSID=… on cookie firmware]
POST /logon.cgi             username=admin&password=<redacted>&cpassword=&logon=Login -> 200 login page errType 0
GET  /SystemInfoRpm.htm     -> 200 data (info_ds) => session verified
GET  /PoeConfigRpm.htm      -> 200 data
POST /poe_port_config.cgi   sel_3=1&name_pstate=1&name_ppriority=3&name_ppowerlimit=4&name_ppowerlimit2=(15.4w)&applay=Apply
GET  /PoeConfigRpm.htm      -> verify state[2]==0
GET  /Logout.htm            -> 200 login page
```

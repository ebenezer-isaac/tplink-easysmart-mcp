# Breaker round 1 — vector parsers-forms — tplink-easysmart-mcp @ e4e8004

Model: claude-opus-4-8 (running agent; the brief named Fable 5.1 for the commit trailer — see note at end)
Branch: breaker/r1-parsers-forms

Claim attacked: "In `tplink-easysmart-mcp` (S1), the page parsers turn every TL-SG1016PE page into a correct pydantic model and never raise on hostile input (…1 MB pages, 10,000 declared ports); the login-page classifier correctly distinguishes a plain login page, the encrypted-login variant, the restored-account/factory-reset page …and never misclassifies a data page as a login page or vice versa; the PoE and port form builders perform read-modify-write …encode state/priority/limit exactly as the reference client …; unit conversions are exact; the `+2` padding is handled; nothing in `switch/` performs I/O."

## Axes with no finding (covered, produced nothing)

- **jsvars parser edges** — truncated/short arrays -> `ProtocolError`; declared counts 0/1/8/16/48 handled; negative/float values rejected by `_int_array`; `0x` hex, empty strings, trailing commas, single quotes, unquoted keys, BOM, CRLF all parse; comments-in-arrays, a bare `new`, unbalanced parens, 10,000-item arrays, a `{` x1000 depth bomb, a >256 KB page and NUL bytes all raise `ProtocolError` before materialising. No crash, no wrong value.
- **`logonInfo` as a string / non-int / missing** -> `ProtocolError` as specified.
- **same variable declared twice** -> last-wins, identical to the reference regex. Not a defect.
- **forms RMW round-trip** — PoE and port bodies equal `MANIFEST.rmw_write_examples` exactly, including field order, `name_pstate` 2/1, `name_ppriority` = read+1, limit codes 1-6, `(15.4w)`/`25.5`; exactly one `sel_<n>`; `cpassword` always `""`; `plan_redacted` hides the password; ports 0/9/17/-1 rejected by the input model; manual-equals-preset rewrite is the documented behaviour. No finding.
- **stale read -> form** — builders are pure over the passed `PoePort`/`PortState`, so a stale snapshot is reproduced faithfully; staleness is an S3 concern.
- **arithmetic / units** — tenths-of-W/V and raw-mA conversions match the reference and the manifest; float results compare equal and render correctly. "exact" holds to float equality, with no observable wrong value.
- **fail-open in parsers** — no `except Exception` returning a default; LOGIN_PAGE -> `SessionExpired`, short/missing/ill-typed arrays -> `ProtocolError`, no partial rows, PoE `state` outside {0,1} raises. The single fail-open found is F2.
- **I/O in `switch/`** — grep for socket/httpx/requests/urllib/aiohttp/`open(` finds nothing (the `_open` hits are a local closure name in `jsvars`). Claim holds.
- **reference value-diff** — units, priority (+1), `powerlimit`/`pdclass` mapping and the `+2` padding all match the reference's semantics. Only `AUTO_LIMIT2` diverges on the wire (F5, documented).
- **classifier "vice versa"** — a login page is never read as DATA (login check runs first). Only data->login misfires: F1.

---

## F1 Data page misclassified as the login page — Impact 3 (a valid read becomes a permanent `SessionExpired`/`SESSION_LOST`; evidence: tests + interactive raise) x Likelihood 2 (a user/attacker-set VLAN name or device description containing the 13-char marker `var logonInfo`, which fits the 16-char VLAN-name limit; adversary-reading: yes) = 6 Major

Test: tests/breaker/r1/parsers_forms/test_classify_collisions.py (3 tests)

What happens: `pages.is_login_page` returns true when the whole body contains the bare substring `var logonInfo` or `action="/logon.cgi"`, including inside a quoted string value in the first `<script>` block. A VLAN named `var logonInfo` (13 chars) or a device description holding the marker makes `classify` return `LOGIN_PAGE` for a page that still carries its real anchor variable, so `parse_vlans` / `parse_system_info` raise `SessionExpired`. In the S2/S3 flow this becomes one re-login + re-GET then a second login page -> `SESSION_LOST`: the read can never succeed until a human renames the VLAN. Confirmed interactively: a VLAN named `var logonInfo` -> `classify == LOGIN_PAGE`, `parse_vlans` raises `SessionExpired`; same for a single-quoted VLAN name carrying `action="/logon.cgi"`.

Why (root cause hypothesis): the classifier keys on a substring over the entire body instead of on a `var logonInfo =` / form declaration at script-statement level, so reflected user data collides with the login markers.

---

## F2 `parse_vlans` does unbounded O(portNum) work on an uncapped declared port count — Impact 3 (the single-threaded, serialised client blocks ~5 s at portNum=1,000,000 and ~108 s at 5,000,000, holding the serial lock; uncapped, so large values hang it until killed, and it returns a nonsense model; evidence: measured) x Likelihood 2 (portNum is read from the device's VLAN page, injectable by a LAN host over the switch's cleartext HTTP; adversary-reading: yes) = 6 Major

Test: tests/breaker/r1/parsers_forms/test_vlan_portnum_dos.py (2 tests)

What happens: the port-count pages are protected from a huge declared port count because their per-port arrays must be >= `max_port_num` and `jsvars` caps any array at 4,096 items. `parse_vlans` is not: `portNum` is a scalar int that escapes the array cap, and `_ports_from_mask(mask, port_count)` iterates `range(1, port_count + 1)` once per VLAN, evaluating `1 << (p - 1)` — a bignum that grows with `port_count`. A crafted VLAN page that declares a large `portNum` with tiny member arrays forces super-linear CPU and returns a `VlanTable` with `port_count` far above the hardware. Measured: portNum 800_000 -> 3.5 s, 1_000_000 -> 5.8 s, 1_200_000 -> 7.1 s, 5_000_000 -> 108 s, each returning a model rather than raising. The deterministic test asserts an absurd portNum (100,000) is rejected (it is not); the timing test asserts sub-second parsing.

Why (root cause hypothesis): `portNum`/`port_count` has no upper bound and bypasses the jsvars array cap that guards every other declared length.

---

## F3 Encrypted-variant false positive from a marker inside a comment — Impact 3 (login refused with `AUTH_VARIANT_UNSUPPORTED`; fail-closed/safe direction) x Likelihood 1 (no evidence a real plain page carries an ENCRYPTED_MARKER in a comment; only a crafted page reaches it) = 3 Minor

Test: tests/breaker/r1/parsers_forms/test_login_classifier_false_positives.py::test_encrypted_marker_in_comment_is_not_the_encrypted_variant

What happens: `_auth_variant` flags `ENCRYPTED` if any marker (`encryptType`, `cryp_new`, `plain_password`, `g_tid`, `securityEncode`) appears anywhere in the body, so a plain page mentioning `securityEncode` only in an HTML comment is classified `encrypted` and login is refused. The sibling `account_restored` detection took care to match `var account_restored=1` (to dodge the static `=0`/`=1` text); the encrypted path got no equivalent care.

Why (root cause hypothesis): marker detection is a bare substring scan rather than a check on an actual `var` / `<script src>` declaration.

---

## F4 Restored-account false positive from `value="Confirm"` substring — Impact 3 (normal-mode login refused via `RestoredAccountMode`; fail-closed) x Likelihood 1 (needs a plain page whose static HTML ships a hidden `value="Confirm"` input; common in TP-Link templates but absent from the shipped fixture) = 3 Minor

Test: tests/breaker/r1/parsers_forms/test_login_classifier_false_positives.py::test_static_confirm_input_is_not_restored_account

What happens: `_login_mode` returns `RESTORED_ACCOUNT` if `value="Confirm"` appears anywhere, so a normal errType-0 page carrying a hidden Confirm-password input (revealed by JS only when errType==6) is misread as factory-reset mode, and `build_login_form` then raises — the client can never log in. Note the dangerous direction is safe: a genuine restored page has `logonInfo[0]==6`, caught independently, so the guard never silently POSTs into restored mode and sets the admin password. Only this annoying false-positive direction is fragile.

Why (root cause hypothesis): same as F3 — a substring scan instead of structural detection (a Confirm submit tied to the active, non-hidden form).

---

## F5 `AUTO_LIMIT2` diverges from the reference client — Impact 2 x Likelihood 1 = 2 Won't-fix

No test (documented divergence, not a regression). The builder sends `name_ppowerlimit2=""` for an auto limit, where the reference (`PoePowerLimit.AUTO -> (1, None)` posted through aiohttp) puts the literal `None` on the wire. This is flagged `# S0: confirm` in `constants.py`, the spec says the default is `""` and S0 resolves it, and the claim's enumerated encodings (state 2/1, priority +1, limit codes 1-6) are all correct. Recorded only so S0 settles the exact bytes.

---

### Note on model / trailer

This agent ran on **claude-opus-4-8**. The brief specified the commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; that exact trailer is used on the commit as instructed, but the true running model is recorded here per the breaker protocol.

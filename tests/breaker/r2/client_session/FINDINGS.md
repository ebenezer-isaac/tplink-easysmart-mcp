# Breaker round 2 — vector client-session — tplink-easysmart-mcp @ 82c2c7e

Model: claude-opus-4-8 (running agent; the brief named Fable 5.1 for the commit trailer — see note at end)
Branch: breaker/r2-client-session

Claim attacked: "`SwitchClient`/`SwitchAuthenticator` always `GET /` first with a cookie jar and classify the page before any POST; never POST on the restored-account/encrypted/`SESSION_BUSY` page; send exactly one cleartext `POST /logon.cgi` per explicit login; errType 1/2/6 trip the breaker and never retry; persist a 5-minute cooldown for 3/4/5 across processes; treat errType 0 as unconfirmed until `SystemInfoRpm.htm` returns data; re-login at most once when a data page comes back as login; `GET /Logout.htm` after every mutating call and on context exit; serialise all I/O; a reset after a POST -> `OutcomeUnknown`, never resend; honour `LOGIN_DISABLED`; the file-backed breaker is atomic, 0600, fail-closed; `protected_ports` required when writes are on; unknown `EASYSMART_*` keys fail loudly; the password / `logon.cgi` body never appear in logs, repr, exceptions or envelopes."

## Axes attacked with no finding (covered, produced nothing)

- **State machine — core login/confirm/retry.** One `POST /logon.cgi` per `login()`; errType 1/2/6 and unknown codes trip the breaker and the next `login()` makes no network (`_gate` -> `breaker.check`); errType 3/4/5 record the cooldown and raise; errType 0 confirmed via `GET /SystemInfoRpm.htm`, login-page confirm -> `LOGIN_NOT_ACCEPTED` + cooldown (issue #49); a POST reset -> confirm only, never a re-POST; `ip_bound_active` / restored / encrypted all fail closed with zero POSTs. All exercised by the existing `test_auth.py`; nothing broke them.
- **re-login triggered twice in one call.** `_get_page_impl` has a single re-login branch; a second login page -> `SESSION_LOST`, never a third login. (But see F2 for the *trigger* being wrong.)
- **`OutcomeUnknown` then the next call.** `submit` raising `OutcomeUnknown` leaves `_authenticated` unchanged; the next `get_page` re-reads under the lock and re-logins once if evicted. Correct "re-read, don't assume".
- **Serialisation / concurrency on one client.** Every public entry (`probe/login/logout/get_page/submit`) runs under one non-reentrant `SerialLock`; internal re-login calls `self._auth.login()` (not `self.login()`), so no self-deadlock and no interleaved second POST. `asyncio.gather` of two reads -> one login (existing `test_concurrent_reads_are_serialised`).
- **`LOGIN_DISABLED`.** `_gate` raises `LockoutGuard` before any I/O; `check-auth --login` -> `login_once` -> `login()` hits the same gate; `finally: logout()` is a no-op when not authenticated. No POST.
- **Fail-open on odd `GET /` bodies.** Empty body / data-with-empty-jar -> `SESSION_BUSY` (no POST); 302 (no redirect follow) and non-200 -> `TransportError` before any POST; `H_P_SSID` + data both present -> `cookie` model wins (safe).
- **Config boundaries.** `PROTECTED_PORTS` empty-string and unset both raise `ConfigError` when `ALLOW_WRITES=true`; `VERIFY_TLS`/`TLS_FINGERPRINT_SHA256` rejected; unknown `EASYSMART_*` keys (incl. singular typos) rejected by `reject_unknown_env`; 17-char / spaced password rejected at startup so it never reaches the wire.
- **Encoding of the POST body.** `urlencode(fields)` encodes `&`/`=`/`%`/unicode correctly; `cpassword=""`, `logon=Login`; field order fixed. Exact bytes correct.
- **Secrets in logs / envelopes / state files.** `_do` logs method+path only ("body omitted") — existing `test_logs_never_contain_password_or_cookie` holds; envelope `error.details()` carry only `err_type`/`retry_after_s`; the breaker/cooldown files hold a timestamp and an error code, no credential; `SecretStr` masks the password in `repr(settings)`. (The one leak is F3, a repr of the plan object, not settings.)
- **Cross-process durability.** Breaker/cooldown files are per-device, atomically written (`mkstemp`+`os.replace`), `chmod 0600`; the cooldown persists across instances (existing `test_cooldown_file_persists_across_instances`). Two processes are not mutually excluded at login time beyond the `SESSION_BUSY` probe — this is inherent to the IP-bound protocol and not promised by the claim, so no finding.

---

## F1 Login cooldown is unbounded and wall-clock-based — a future/garbage timestamp or a backward clock step locks login out far beyond 300 s, with no documented recovery — Impact 3 (login is refused for as long as the clock jump / bad value, the server stops serving every tool that needs a session, and the one documented fix `breaker --clear` does not touch the cooldown; evidence: 3 tests) x Likelihood 2 (a backward wall-clock step — NTP correction on a LAN server, an RTC-less host like a Pi booting then syncing, a VM/container clock jump — is a real, recorded event; adversary-reading: no) = 6 Major

Test: tests/breaker/r2/client_session/test_cooldown_clock.py::test_future_timestamp_is_clamped_to_the_window
Test: tests/breaker/r2/client_session/test_cooldown_clock.py::test_backward_wall_clock_step_does_not_extend_cooldown
Test: tests/breaker/r2/client_session/test_cooldown_clock.py::test_breaker_clear_recovers_a_future_timestamp_lockout

What happens: `LoginCooldown.remaining()` returns `max(0, ceil(seconds - (now - last_failure_at)))` with **no upper clamp**, and reads `now` from the wall clock (`time.time`). If `last_failure_at` is in the future the term `now - last` goes negative and `remaining` grows without bound. Two concrete ways to reach that: (a) a genuine soft failure (errType 3/4/5) is recorded, then the wall clock is stepped **backward** (NTP/manual/VM) — the test steps back one hour and `remaining()` reports ~3900 s instead of <=300 s; (b) a valid-JSON numeric `last_failure_at` in the future survives the "corrupt => fail closed" guard (which only rejects non-numeric values), so `remaining()` returns ~10,000,000 s and `client.login()` raises `CooldownActive(retry_after_s=10_000_300)` before any network. Worse, the only documented operator action, `tplink-easysmart-mcp breaker --clear`, calls `backend.breaker.clear()` only and never deletes the cooldown file (there is no `cooldown --clear`), and a confirmed login — the only other clear path — can never run because the cooldown blocks it. The lockout is escapable only by hand-deleting the state file.

Why (root cause hypothesis): `remaining()` lacks a `min(seconds, ...)` clamp and the cooldown mixes a wall clock with a durable timestamp; a monotonic/duration model, or at least clamping to `[0, seconds]`, would bound it, and there is no CLI to clear the cooldown.

---

## F2 A classifier false positive on a data page forces a spurious re-login (evicting the owner's live session) and then an unrecoverable `SESSION_LOST` — Impact 3 (a valid read becomes a permanent failure until a human edits the device, and each attempt spends a login that kicks the owner's web UI off this single-session switch; evidence: 2 tests + r1 F1) x Likelihood 2 (the login marker `var logonInfo` is 13 chars and fits the switch's 16-char VLAN-name / description limits, so it is settable by anyone who can reach the cleartext web UI or an MCP write; adversary-reading: yes) = 6 Major

Test: tests/breaker/r2/client_session/test_classifier_session_hazard.py::test_reflected_marker_data_page_still_reads
Test: tests/breaker/r2/client_session/test_classifier_session_hazard.py::test_reflected_marker_does_not_cost_a_second_login

What happens: round 1 (F1, `breaker/r1-parsers-forms`) proved `pages.is_login_page` decides LOGIN_PAGE by a bare substring scan of the whole body for `var logonInfo` / `action="/logon.cgi"`, so a reflected user string (a VLAN name, the system description) flips a valid DATA page to LOGIN_PAGE. This test drives the *session* consequence through `SwitchClient.get_page`: a perfectly valid, parseable PoE page that merely contains the marker string is classified LOGIN_PAGE, so the client calls `mark_logged_out()`, performs a **second** `POST /logon.cgi`, re-GETs the same body, classifies it LOGIN_PAGE again, and raises `SESSION_LOST` (`the /PoeConfigRpm.htm page returned the login page twice`). So an ordinary read (1) spends a second login POST — and spec section 3.5 says every MCP login evicts the owner's browser session on this one-effective-session switch — and (2) can never succeed until the offending VLAN/description is renamed. The re-login-at-most-once rule holds mechanically, but it is fired on the wrong signal.

Why (root cause hypothesis): the session state machine trusts `classify`'s substring-based LOGIN_PAGE verdict as "evicted"; with the r1 substring classifier, reflected device data is indistinguishable from an actual login page, so a read triggers an eviction-and-relogin it should never trigger.

---

## F3 The login `RequestPlan` renders the cleartext password in `repr`/`str` — Impact 4 (a password written where the claim says it never is; one `log.debug("%r", plan)` or an exception embedding the plan would persist the admin password; evidence: 2 tests) x Likelihood 1 (no current code path repr()s or logs the raw login plan — the leak is latent; adversary-reading: no) = 4 Minor

Test: tests/breaker/r2/client_session/test_secrets_repr.py::test_login_plan_repr_does_not_leak_password
Test: tests/breaker/r2/client_session/test_secrets_repr.py::test_login_plan_str_does_not_leak_password

What happens: the claim says the password "never appear[s] in logs, repr, exceptions or envelopes." `forms.build_login_form` returns a frozen `RequestPlan` whose `fields` tuple is `(("username", u), ("password", <cleartext>), ("cpassword", ""), ("logon", "Login"))`. `RequestPlan` is a plain dataclass, so `repr(plan)`/`str(plan)` print every field, password included (`...('password', 'Pw1&=%x9')...`). `plan_redacted` produces the safe shape but is applied only on the dry-run/logging path; the raw object returned by `build_login_form` and held across the in-flight `raw_post` is the un-redacted one. The leak is latent today (nothing repr()s it), hence likelihood 1, but the invariant the claim states is literally false.

Why (root cause hypothesis): the password travels as a bare `str` inside a reprable dataclass instead of a masked type (e.g. `SecretStr`) or a plan whose `__repr__` redacts secret-named fields.

---

### Note on model / trailer

This agent ran on **claude-opus-4-8**. The brief specified the commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; that exact trailer is used on the commit as instructed, while the true running model is recorded here per the breaker protocol (section 1, "every report states the model ID it ran on").

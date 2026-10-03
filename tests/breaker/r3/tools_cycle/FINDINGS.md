# Breaker round 3 - vector tools-cycle - tplink-easysmart-mcp @ 99103cd

Model: claude-opus-4-8
Branch: breaker/r3-tools-cycle - worktree: E:/projects-working-dir/.wt/tplink-easysmart-mcp/breaker-r3-tools-cycle
Tests: tests/breaker/r3/tools_cycle/test_*.py (fakes only; one multiprocessing spawn test; no network).
Run: `.venv/Scripts/python.exe -m pytest tests/breaker/r3/tools_cycle/ -q`  ->  **12 failed, 2 passed** (the 2 passing are labelled controls, not findings).

## Claim attacked

"No mutating cgi request leaves the process unless EASYSMART_ALLOW_WRITES=true AND confirm_write=True; dry-run sends nothing and returns the exact form; switch_set_poe/switch_set_port read live, rebuild the FULL per-port tuple so priority/limit can never be clobbered, select exactly one port, verify after the write; switch_poe_cycle can never act on a protected / non-PoE / unresolvable-or-ambiguous port; never runs two cycles at once (asyncio lock + crash-visible marker; a marker younger than 5 min refuses, older proceeds); after turning a port off it ALWAYS attempts to turn it back on, on every failure path (exception, breaker trip, eviction, timeout, cancellation), and reports both outcomes; off_seconds is bounded; every write logs out; a connection reset after a POST yields OUTCOME_UNKNOWN settled by re-reading, never resending; and no secret appears in any envelope or log."

**Verdict: DISPROVEN.** The "ALWAYS turns the port back on, on every failure path" guarantee is false (F1, F2 - device-destructive), the "never two cycles at once" cross-process guarantee is false (F3), "unambiguous port selection" is false (F5), and "confirm_write=True (boolean)" is not enforced at the real tool boundary (F6).

## Axes with no finding (confirmed solid, or no failing test produced)

- Dry-run sends nothing and returns both redacted forms (existing test_cycle.py::test_dry_run_* holds; re-verified).
- switch_set_poe/switch_set_port RMW + verify + WRITE_VERIFY_FAILED on a clobbered priority/limit/speed: correct. (A concurrent EXTERNAL change between read and write is silently reverted and not flagged, but that is inherent to RMW on a single-session device - not a finding.)
- Both write gates before any network call; ALLOW_WRITES=true + empty PROTECTED_PORTS -> ConfigError at startup: correct.
- off_seconds bounds (0/4/121/NaN/-1/str, bool) rejected before any network: correct; at the real boundary FastMCP coerces to int first, still rejected.
- Protected / non-PoE (config-and-live poe_port_num) / already-off guards; uplink-by-name -> protected -> refused; case/whitespace name resolution; duplicate map-name rejection: correct.
- PoE-capable-but-unpowered (class "--") port: enabled is admin state, so it cycles and ends in POWER_NOT_RESTORED if no PD draws - per spec, not a finding.
- Breaker trip / single eviction during the sleep/on steps: handled (existing tests). The dangerous variant is the off-verify window (F1), not the sleep.
- Secrets: the cycle builds only PoE forms (no password); plan_redacted/client never log a body; no password/cookie in the cycle envelope (existing test_cycle_envelope_has_no_password + manual DEBUG sweep). No finding.
- Marker unwritable dir: begin() raises OSError, but run_tool converts it to INTERNAL_ERROR and no device is touched (the marker is claimed before any network) - fail-closed, not scored.
- ON-path resend after reset: _best_effort_on resends the on form after OUTCOME_UNKNOWN; technically "resends" but re-enabling power is the intended retry and is benign - Won't-fix (1).

---

## F1 crash-safety hole: the OFF window sits outside the restore guard - Impact 4 (a real camera is left UNPOWERED and the envelope is a bare TRANSPORT_ERROR/OUTCOME_UNKNOWN, not "may be UNPOWERED", so the owner is not even warned - evidence: probe + test, port state[0]==0, 1 POST, no on-write) x Likelihood 2 (a connection reset - which this switch does, hence the whole OUTCOME_UNKNOWN design - or an owner web-UI eviction landing on the off-verify read; the trigger is a documented real event, the specific timing is the uncommon part; adversary-reading: no) = 8 Major

Test: tests/breaker/r3/tools_cycle/test_off_window_no_restore.py::test_reset_on_off_verify_read_still_attempts_restore and ::test_reset_after_off_post_lands_is_settled_by_reread_and_restore

What happens: In cycle.py::_run_cycle, the off submit AND the off-verify re-read run BEFORE the try/except DeviceError that guarantees a restore:

    await client.submit(off_form)                      # (A) reset-after-landing -> OUTCOME_UNKNOWN
    off_state = (await client.poe()).ports[port - 1]   # (B) reset / evict-twice / malformed page
    if off_state.enabled: raise WriteVerifyFailed(...)
    off_at = now()
    try: ... turn ON / poll ...
    except DeviceError: _best_effort_on(...)           # only reachable from inside the try

When the off write lands (port OFF) and either (A) the POST connection is reset after landing, or (B) the very next PoE read errors, the DeviceError propagates straight out of _run_cycle. No ON write is attempted, the camera stays dark, and the envelope is OUTCOME_UNKNOWN/TRANSPORT_ERROR - never CYCLE_INCOMPLETE "may be UNPOWERED". The claim "after turning a port off it ALWAYS attempts to turn it back on, on every failure path (... eviction, timeout ...)" and "a reset after a POST is settled by re-reading" are both false for the off step.

Why (root cause hypothesis): off_at = now() is placed after the off-verify read, so the code treats "the port is confirmed off" as the start of the danger window, but the write that CAUSES the outage happens one line earlier; the two statements that can leave the port dark are outside the restore try. (Do not fix.)

## F2 cancellation / non-DeviceError between off and on skips the restore - Impact 4 (camera left dark; CancelledError also escapes run_tool, breaking "every tool returns an envelope / never raises") x Likelihood 2 (an in-flight tool call cancelled by client disconnect / timeout / shutdown during the 10-120 s off window - a normal asyncio event; adversary-reading: no) = 8 Major

Test: tests/breaker/r3/tools_cycle/test_cancellation_no_restore.py::test_cancellation_between_off_and_on_still_restores_power and ::test_non_device_exception_between_off_and_on_still_restores_power

What happens: _run_cycle's restore guard is `except DeviceError`; run_tool is `except Exception`. asyncio.CancelledError is a BaseException and a plain RuntimeError/IndexError is not a DeviceError, so either one raised between the off and the on (the test raises it from the injected off-window sleep) unwinds past _best_effort_on. The port is left off (state[0]==0, zero on-writes); the cancel propagates out of the tool, and the generic exception surfaces as a bare INTERNAL_ERROR, not CYCLE_INCOMPLETE.

Why (root cause hypothesis): the "always restore" promise is implemented as a narrow `except DeviceError` instead of a finally / `except BaseException`-with-re-raise around the off->on window; anything that is not a DeviceError bypasses it. (Swallowing a cancel TO restore power is a defensible fix - the claim just asserts a behaviour the code does not have. Do not fix.)

## F3 the crash-visible marker has no cross-process lock (ROOT-CAUSE S4 decide-then-record) - Impact 3 (the advertised cross-process "one cycle at a time" guarantee is void; two OS processes drive the same port's off/on with no mutual exclusion, compounding F1/F2) x Likelihood 1 (two concurrent switch processes is not the single-service deploy; witnessed only in test; adversary-reading: no) = 3 Minor

Test: tests/breaker/r3/tools_cycle/test_marker_multiprocess_race.py::test_two_processes_both_claim_the_same_marker (6 spawned processes; all 6 pass begin() - evidence: results=['OK'] x6)

What happens: CycleMarker.begin is "existing = self._read(); ...freshness...; self._write(...)" with no file lock. The asyncio.Lock in poe_cycle_op is per-process and cannot see another process, so the marker is the only cross-process guard. Six processes released by a multiprocessing.Barrier all read "no marker", all write, all proceed - the marker enforces nothing across processes. This is exactly the structural pattern the round-2 root-cause doc S4 predicts ("read is-a-cycle-running?, decide, then write the marker - two overlapping calls both read no and both start").

Why (root cause hypothesis): the in-flight invariant is split across two stores (an in-memory asyncio.Lock and an unlocked state file) reconciled by a non-atomic read-then-write; only a cross-process advisory lock on the compound operation can hold it. (Do not fix.)

## F4 a future-dated marker / backward clock jump wedges switch_poe_cycle forever - Impact 3 (a marker that can never clear: the tool is unusable until a human deletes the file) x Likelihood 1 (corrupt/tampered/future marker or a backward wall-clock step coinciding with a leftover marker; test-only; adversary-reading: no) = 3 Minor

Test: tests/breaker/r3/tools_cycle/test_marker_clock_wedge.py::test_future_timestamp_marker_wedges_every_cycle and ::test_backward_clock_jump_wedges_an_existing_marker

What happens: begin() refuses while age = now() - started_at < 300. A future started_at (corrupt file, NTP/DST step, or a backward clock jump after a crash left a marker) makes age permanently negative, so age < 300 is always true and every cycle is refused as CYCLE_IN_PROGRESS (evidence: age_s == -9_999_000). Note the asymmetry: an unreadable/non-dict marker fails OPEN (_read -> {"started_at": None} -> stale -> proceeds) but a structurally-valid marker with a nonsensical number fails CLOSED and self-wedges.

Why (root cause hypothesis): freshness is computed from an unvalidated on-disk float with no clamp/sanity bound and no "impossible age => treat as stale" branch, so a single bad timestamp is indistinguishable from "fresh forever". (Do not fix.)

## F5 a numeric PORT_MAP name shadows the literal port - the wrong camera is cycled - Impact 4 (a real, unintended camera is power-cycled/disabled while the envelope reports the requested value) x Likelihood 2 (needs a numeric map name - which config._PORT_NAME = [A-Za-z0-9_.-]{1,32} accepts silently - plus a string argument, which an LLM routinely produces; adversary-reading: YES - the LLM supplies the port argument, so the string form is highly reachable, capped only by the operator having a numeric name) = 8 Major

Test: tests/breaker/r3/tools_cycle/test_portmap_numeric_name.py::test_numeric_name_string_resolves_to_a_different_port_than_the_int and ::test_cycle_with_string_3_acts_on_port_5_not_port_3

What happens: With EASYSMART_PORT_MAP="3=5", resolve_port checks the name map BEFORE the digit-string branch, so resolve_port("3") -> port 5 (the camera NAMED "3") but resolve_port(3) -> port 3. The same logical value resolves to a different physical port by argument type alone; switch_poe_cycle("3") drives sel_5 (evidence: cycled ports == {5}). The claim's "selects exactly one port" / "unresolvable or ambiguous name is refused" is not met - the ambiguity resolves silently to the name.

Why (root cause hypothesis): resolve_port gives name lookup precedence over the integer-literal interpretation and the config layer permits purely-numeric names, so int-vs-string of the same token maps to two ports with no ambiguity check. (Do not fix.)

## F6 confirm_write truthy-string coercion defeats the write gate at the real boundary - Impact 2 (a mutating cgi POST leaves the process on a value the gate's own contract says must be refused; the "truthy strings do not count" defence is dead code; practical loss is bounded because the caller's string is affirmative and falsey strings still refuse) x Likelihood 3 (LLM clients routinely send JSON strings for booleans, on the ordinary path; adversary-reading: YES - the LLM supplies confirm_write) = 6 Major

Test: tests/breaker/r3/tools_cycle/test_confirm_write_coercion.py::test_truthy_string_confirm_write_lets_the_write_through[true|1|yes] (control test_falsey_string_is_still_refused[false|0] passes)

What happens: core/write_gate.check_write_gate enforces `confirm_write is not True` and its docstring says "truthy strings do not count" (master-plan non-negotiable #4: the boolean True). But the FastMCP tool parameter is typed `confirm_write: bool`, so pydantic coerces "true"/"1"/"yes" to True BEFORE the gate runs. Through the real mcp.call_tool boundary, switch_set_poe(port=1, enabled=False, confirm_write="yes") performs the POST (evidence: 1 POST, success=True). This affects switch_set_poe/switch_set_port/switch_poe_cycle alike.

Why (root cause hypothesis): the strict-boolean check lives one layer BELOW the schema that already coerced the value, so by the time the gate runs the string is indistinguishable from a real boolean; enforcing it requires a strict / Literal[True] param type or re-reading the raw argument. (Do not fix.)

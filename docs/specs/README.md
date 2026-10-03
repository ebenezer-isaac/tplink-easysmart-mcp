# Specs

Design and protocol documents for `tplink-easysmart-mcp`. These record *why* the
server is built the way it is and *what the switch's web protocol does*; the
runtime behaviour is the code and its tests.

| File | What it is |
|---|---|
| [`00-MASTER-PLAN.md`](00-MASTER-PLAN.md) | Design context: why the device-agnostic `core/` is written once and copied verbatim across a family of standalone MCP servers, the non-negotiables (secrets, lockout safety, write gating, envelope, redaction), and the phase contract. |
| [`02-SWITCH-SPEC.md`](02-SWITCH-SPEC.md) | The build spec for this repo: protocol summary, configuration table, and the phased plan (parsers → client → tools → live verification). |
| [`04-BREAKER-PROTOCOL.md`](04-BREAKER-PROTOCOL.md) | The adversarial review protocol for the lockout breaker — how breaker rounds are run and when a repeated finding becomes a design finding. |
| [`05-ROOT-CAUSE-breaker.md`](05-ROOT-CAUSE-breaker.md) | The root-cause analysis that produced the single canonical breaker/state design (one locked, schema-validated, atomically-written ledger; fail-closed throughout). |
| [`switch-protocol.md`](switch-protocol.md) | The authoritative read-only study of the Easy Smart web protocol: transport, inline-variable extraction, authentication, read pages, write endpoints, power-cycle. |
| [`switch-endpoints.json`](switch-endpoints.json) | The machine-readable endpoint map (pages, `*.cgi` handlers, variables, fields, confidence tags). |

Implementation-level notes that track the code live in
[`../protocol/easysmart-switch.md`](../protocol/easysmart-switch.md). Facts that a
live on-device capture (phase S0) must still confirm are tagged `# S0: confirm` in
`tplink_easysmart_mcp/switch/constants.py`.

> These documents use only RFC 5737 (`192.0.2.x`) and RFC 7042
> (`00:00:5E:00:53:xx`) documentation placeholders — no real network data.

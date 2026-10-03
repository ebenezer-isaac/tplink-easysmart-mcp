# Contributing

Thanks for your interest. This is a small, safety-focused project; a few rules keep
it that way.

## Ground rules

- **Never point the test suite or development at a real switch.** The whole suite
  runs offline against synthetic fixtures (`respx` for HTTP, HTML fixtures under
  `tests/fixtures/`). On-device verification is a separate, deliberate step run by a
  maintainer with the hardware present.
- **No secrets, ever.** No real IPs, MACs, serials, hostnames, or site/camera/
  person names in code, tests, docs, or commit messages. Use RFC 5737 (`192.0.2.x`)
  and RFC 7042 (`00:00:5E:00:53:xx`) documentation placeholders. The secret scanner
  enforces this.
- **No behaviour change without a test.** Tests exist to break the code; prefer
  edge-case, adversarial, and state-machine tests over happy-path ones.

## Development setup

```bash
python -m venv .venv
# Windows:
.venv/Scripts/python.exe -m pip install -e ".[dev]"
# POSIX:
# source .venv/bin/activate && pip install -e ".[dev]"
```

## The gate

`scripts/gate.py` is the single source of truth for "is this commit OK". **It must
print `PASS` before every commit**, and CI runs it on Ubuntu and Windows across
Python 3.11–3.13.

```bash
python scripts/gate.py
```

It runs, in order:

1. `ruff check` and `ruff format --check`.
2. `pytest` with coverage thresholds: `core/` ≥ 90 %, `switch/` ≥ 90 %, package ≥ 80 %.
3. `scripts/check_no_secrets.py` — blocks private IPs, MACs, and credential-shaped
   strings (a single line may be exempted with a `secret-scan: allow <reason>`
   marker, used sparingly for documented test vectors).
4. A stub scan — no `NotImplementedError`, `TODO`, `FIXME`, or bare `...` bodies
   under the package.
5. `tplink-easysmart-mcp --list-tools` exits 0 and lists `switch_status`.

## Commit and PR conventions

- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`,
  `perf:`, `ci:`.
- Keep files roughly 200–400 lines (800 hard max); prefer new immutable objects
  over mutation; handle errors explicitly (never swallow); validate at boundaries.
- A PR should describe what changed and include a test plan; the gate must pass.

## The lockout breaker protocol

Because the switch locks the admin account and is single-session, the breaker is
not optional plumbing — it is the safety contract. When working on auth/session:

- A failed login must **trip the breaker and never retry**; the server stays
  refused until a human runs `tplink-easysmart-mcp breaker --clear`.
- The admit decision and the failure record are one atomic, cross-process-locked,
  schema-validated write — never a pure read followed by a later write. Any store
  failure (unwritable, corrupt, wrong type) must **fail closed**.
- Session-busy / timeout (errType 3/4/5) is a cooldown, not a breaker trip.
- The breaker contract is exercised by `tests/conformance/test_breaker_contract.py`.
  Do not weaken, skip, or fork a conformance case to make something pass. See
  `docs/specs/04-BREAKER-PROTOCOL.md` and `docs/specs/05-ROOT-CAUSE-breaker.md`.

## The core-identity rule

`tplink_easysmart_mcp/core/` is a device-agnostic template whose **canonical copy
lives in the sibling `vigi-nvr-mcp` project** and is copied **verbatim** here. It is
pinned by `core/VERSION` (a semver plus a SHA-256 manifest) and guarded by
`tests/test_core_identity.py`, which fails the gate on any drift.

Therefore:

- **Do not edit files under `core/` in this repo to fix a bug or add a feature.**
  Fix it in the canonical `vigi-nvr-mcp` copy, bump `core/VERSION`, regenerate the
  manifest, and re-copy into all siblings. Device-specific behaviour belongs in
  `tplink_easysmart_mcp/switch/`, never in `core/`.
- The conformance suite (`tests/conformance/`) is likewise copied verbatim and
  hashed.

### The conformance suite is package-agnostic

The verbatim conformance test names no package. It resolves `breaker`/`errors`/`state`
from the one-line `core_pkg` fixture in `tests/conftest.py` (which returns this repo's
`tplink_easysmart_mcp.core`), and its spawned `multiprocessing` workers import the same
package by the path passed through their arguments. No namespace shim is needed.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).

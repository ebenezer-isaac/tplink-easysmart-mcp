# Security policy

## Threat model

This server controls a managed network switch — including turning PoE power on and
off — over a protocol with no transport security. Treat it accordingly.

### The central fact: the login is cleartext

TP-Link Easy Smart switches serve a plain HTTP web UI on port 80. There is no
TLS, and the login form sends the admin **password in cleartext** on every login.
The device exposes no other management interface this project uses (the UDP
discovery protocol is unauthenticated and out of scope; firmware upload is never
called).

Consequences, enforced in code:

- **Run the server only on the wired LAN host that reaches the switch.** The
  cleartext password must never cross WiFi, a VPN hop, Tailscale, or any
  untrusted segment. The deployment unit (`deploy/`) is documented as LAN-host-only.
- **The server has no authentication of its own.** Its MCP HTTP transport binds to
  `127.0.0.1` by default; expose it only over an SSH tunnel or Tailscale to the
  *MCP port*, never by binding the switch-facing side to a routable address. A
  warning is logged if the HTTP transport is bound to a non-loopback address.
- **Secrets live in the environment / env file only**, never in the repo. The env
  file should be mode `0600` and owned by the service account.

### Lockout and denial-of-service self-protection

The switch locks the admin account after repeated bad logins, and is effectively
single-session (a login evicts a human's web-UI session and vice versa). To avoid
locking the operator out of their own switch:

- A failed login is **never retried automatically**.
- Credential/lockout/restored-account failures trip a **persistent breaker** that
  blocks further logins until a human runs `tplink-easysmart-mcp breaker --clear`.
- Session-busy and timeout responses set a **cooldown** instead of tripping.
- The password is validated at startup (length/charset) so a malformed value never
  reaches the device and burns a lockout attempt.

### Write safety

- Writes require **two independent keys** (`EASYSMART_ALLOW_WRITES=true` and
  `confirm_write=true`); either missing means no network call is made.
- `EASYSMART_DRY_RUN=true` returns the exact request bytes without sending them.
- Writes are read-modify-write and re-verified, so a change cannot silently clobber
  an unrelated field.
- `EASYSMART_PROTECTED_PORTS` (required when writes are enabled) prevents any write
  from disabling the uplink or the server's own port — which would lock the server
  out of the switch.
- A PoE power-cycle reserves the port across processes and **restores power on
  every failure path**, reporting if a port may be left unpowered.

### Secret handling

- Credential-bearing fields (`password*`, `passwd*`, `key`, `token`, `stok`,
  `nonce`, `cookie`, `ciphertext`) are recursively redacted from all tool output.
- Login request bodies and cookie values are never logged.
- `scripts/check_no_secrets.py` runs in the gate and in CI to block any private IP,
  MAC, or credential-shaped string from being committed. Only RFC 5737
  (`192.0.2.x`) and RFC 7042 (`00:00:5E:00:53:xx`) documentation placeholders are
  allowed.

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories
("Report a vulnerability" on the repository's **Security** tab) rather than opening
a public issue. Include the firmware/hardware revision, reproduction steps, and the
impact. You will receive an acknowledgement; a coordinated disclosure timeline will
be agreed before any public write-up.

## Supported versions

This is pre-1.0 software; only the latest `main` / latest release receives fixes.

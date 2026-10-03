# Deploying `tplink-easysmart-mcp`

The switch login is **cleartext**. This server MUST run on the wired LAN host that
reaches the switch — never across WiFi, a VPN, or a Tailscale/SSH hop. Reach the
*MCP* over Tailscale/SSH by binding its HTTP transport to loopback; keep the
switch-facing traffic on the local wire. See [`../SECURITY.md`](../SECURITY.md).

The example unit [`tplink-easysmart-mcp.service.example`](tplink-easysmart-mcp.service.example)
runs the server as a non-root system account, with an `0600` env file, a systemd
`StateDirectory`, loopback HTTP, and a hardened sandbox.

## 1. Install the code

```bash
sudo python3 -m venv /opt/tplink-easysmart-mcp/.venv
sudo /opt/tplink-easysmart-mcp/.venv/bin/pip install \
  git+https://github.com/ebenezer-isaac/tplink-easysmart-mcp
```

## 2. Create the service account and state directory

```bash
sudo useradd --system --home /var/lib/tplink-easysmart-mcp \
  --shell /usr/sbin/nologin easysmartmcp
```

(systemd's `StateDirectory=` creates and owns `/var/lib/tplink-easysmart-mcp` on
first start; you do not need to `mkdir` it.)

## 3. Write the env file (mode 0600)

```bash
sudo install -m 0600 -o easysmartmcp -g easysmartmcp \
  /path/to/.env.example /etc/tplink-easysmart-mcp.env
sudoedit /etc/tplink-easysmart-mcp.env
```

Fill in at least `EASYSMART_HOST` and `EASYSMART_PASSWORD`. Leave
`EASYSMART_ALLOW_WRITES=false` until you have set `EASYSMART_PROTECTED_PORTS` (see
below). Confirm the file is `0600` and owned by `easysmartmcp`.

## 4. Fill `PORT_MAP` and `PROTECTED_PORTS`

These are the two site-specific values the safety model depends on:

- **`EASYSMART_PROTECTED_PORTS`** — the uplink port (the one going to your router)
  **and the port this host itself is plugged into**. No write or power-cycle will
  ever touch them, so you cannot accidentally disable the switch's path to the
  network or to this server. This is **required and non-empty** once
  `ALLOW_WRITES=true`; the server refuses to start otherwise. Example: `16,15`.
- **`EASYSMART_PORT_MAP`** — friendly names for the ports you will manage, e.g.
  `cam_hall=1;cam_gate=2`. A name must start with a letter. These let an agent say
  "power-cycle `cam_hall`" instead of a bare port number.

To find the port numbers safely, run read-only first:

```bash
sudo -u easysmartmcp EASYSMART_HOST=<ip> EASYSMART_PASSWORD=<pw> \
  /opt/tplink-easysmart-mcp/.venv/bin/tplink-easysmart-mcp check-auth --login
# then use switch_get_ports / switch_get_poe from your MCP client to map
# each physical device to its port number, and note the uplink and this host's port.
```

## 5. Install and start the unit

```bash
sudo install -m 0644 tplink-easysmart-mcp.service.example \
  /etc/systemd/system/tplink-easysmart-mcp.service
# review ExecStart / paths in the unit, then:
sudo systemctl daemon-reload
sudo systemctl enable --now tplink-easysmart-mcp
systemctl status tplink-easysmart-mcp
```

The service listens on `127.0.0.1:8766` (streamable-HTTP) by default. Point your
MCP client at it over an SSH tunnel or Tailscale.

## 6. (Optional) First live capture — S0

Before trusting writes on an untested unit, capture the switch's real pages once to
confirm the protocol facts tagged `# S0: confirm` in
`tplink_easysmart_mcp/switch/constants.py` (the auto power-limit field, array
padding, the disabled-PoE read value, whether `Logout.htm` ends the session, and
the VLAN page names). Outline:

1. Ensure **no one is logged into the switch web UI** (it is single-session).
2. On the LAN host only, and read-only (`GET *.htm`; the sole POST is one
   `logon.cgi`), fetch `/`, then `SystemInfoRpm.htm`, `PortSettingRpm.htm`,
   `PortStatisticsRpm.htm`, `PoeConfigRpm.htm`, `Vlan8021QRpm.htm`,
   `Vlan8021QPvidRpm.htm`, and `Logout.htm`. **Never** fetch any page whose name
   contains `Reboot`, `Reset`, `Upgrade`, `Saving`, `Backup`, or `Restore`.
3. **Sanitise** the captures outside the repo: replace the MAC with
   `00:00:5E:00:53:10`, every IPv4 (except `255.*`/`0.0.0.0`) with `192.0.2.x`,
   the device description with `TL-SG1016PE`, VLAN names with `vlanN`, and cookie
   values with `<redacted>`. Run `scripts/check_no_secrets.py` over the result
   before it goes anywhere near the repo.
4. Diff against `docs/specs/switch-protocol.md`; apply any corrections.

The full procedure is in [`../docs/specs/02-SWITCH-SPEC.md`](../docs/specs/02-SWITCH-SPEC.md)
(phases S0 and S4).

## Verifying write safety before enabling writes

1. Start read-only; confirm `switch_get_poe` / `switch_get_ports` look right.
2. Set `EASYSMART_PROTECTED_PORTS`, then `EASYSMART_ALLOW_WRITES=true`, restart.
3. Dry-run a cycle: `EASYSMART_DRY_RUN=true`, then call `switch_poe_cycle` with
   `confirm_write=true`, and inspect the exact off/on forms it would send.
4. Only then run a real power-cycle on a **non-critical** PoE device.

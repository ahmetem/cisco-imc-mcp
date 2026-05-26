# Cisco IMC MCP Server

A local [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server
that lets Claude Desktop (or any MCP-compatible client) manage a
**Cisco UCS C-Series** or **HyperFlex** server through the Cisco Integrated
Management Controller (IMC), using the **XML API (Nuova)** — supported on all
IMC firmware versions: 2.x, 3.x, 4.x.

Tested with a **HX220C-M4S** running IMC firmware **4.1(2m)**.

> 🇹🇷 Türkçe README için: [README.tr.md](./README.tr.md)

## Features

Fourteen tools across three categories:

### Read-only (safe to call automatically)

| Tool | Description |
|---|---|
| `imc_get_system_info` | Vendor, model, serial, CPUs, cores, total memory, BIOS POST state, IMC firmware version |
| `imc_get_power_status` | Current operational power state (on/off) and BIOS POST state |
| `imc_get_health` | Power supplies, fans, and CPU temperature sensor readings |
| `imc_get_psu_details` | Per-PSU model + operability **plus AC input (V/A/W) and DC output (V/A) stats** |
| `imc_list_drives` | Physical drives: slot, vendor, model, size, type, state |
| `imc_get_disk_smart` | Per-drive health & error counters: `pdStatus`, `predictiveFailureCount`, `mediaErrorCount`, `otherErrorCount`, `linkSpeed`. Optional `slot_id` for single-drive detail. |
| `imc_get_memory_health` | Per-DIMM presence, capacity, speed, vendor, operability, operState across all `memoryArray` containers |
| `imc_get_event_log` | System Event Log (SEL) entries (newest first). Optional `max_entries` (default 50) and `severity_filter` (substring match against `critical`/`warning`/`info`/etc.) |

### Power actions (require `confirm=true`)

| Tool | Description |
|---|---|
| `imc_power_on` | Power on the server (`adminPower=up`) |
| `imc_power_off_graceful` | Request graceful OS shutdown via ACPI (`soft-shut-down`) |
| `imc_power_off_force` | Immediately cut power (`down`) — may cause data loss |
| `imc_reboot` | Force restart, hard reset (`hard-reset-immediate`) |
| `imc_power_cycle` | Power cycle: off then on (`cycle-immediate`) |

### Destructive maintenance (require `confirm=true` AND `i_understand_data_loss=true`)

| Tool | Description |
|---|---|
| `imc_clear_event_log` | Clear the System Event Log. Irreversible — all event history is permanently lost. Consider exporting via `imc_get_event_log` first. |

### Built-in safety

Action tools require `confirm=true`. Destructive tools that erase persistent
state (right now only `imc_clear_event_log`) additionally require
`i_understand_data_loss=true`. The agent must explicitly pass both flags —
in practice this means Claude only fires these after the user clearly asks
for the action. Read-only tools have no such guard.

## Requirements

- **Python 3.11+**
- A Cisco UCS C-Series or HyperFlex server with IMC reachable over HTTPS
- IMC credentials (a user with privilege to read inventory and control power)
- Claude Desktop (or any MCP client)

## 1. Install the server

### Windows (PowerShell)

```powershell
git clone https://github.com/<your-username>/cisco-imc-mcp.git C:\mcp-servers\cisco-imc-mcp
cd C:\mcp-servers\cisco-imc-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run this once in an
administrator PowerShell:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Linux / macOS

```bash
git clone https://github.com/<your-username>/cisco-imc-mcp.git ~/mcp-servers/cisco-imc-mcp
cd ~/mcp-servers/cisco-imc-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure `.env`

```powershell
copy .env.example .env
notepad .env
```

Fill in:

```ini
IMC_HOST=192.168.1.106       # IP or hostname of the IMC web interface
IMC_USERNAME=admin           # IMC user
IMC_PASSWORD=replace-me      # IMC password
IMC_VERIFY_SSL=false         # most IMCs ship with self-signed certs
IMC_TIMEOUT=30
IMC_RACK_DN=sys/rack-unit-1  # default; rarely needs changing
```

**Never** commit `.env` to git. The included `.gitignore` already excludes it.

### About `IMC_RACK_DN`

The XML API addresses managed objects by their **Distinguished Name (DN)**.
For a standalone C-Series server (and most HyperFlex single-server nodes),
the rack-unit DN is `sys/rack-unit-1`. If you have a chassis with multiple
rack-units, you may need to set this to `sys/rack-unit-2`, etc.

## 3. Smoke test

With the venv active:

```powershell
python cisco_imc_mcp.py --help
```

You should see the tool list and exit cleanly. Any import error means a
dependency didn't install.

If you have a helper diagnostic file `_diag.py`, you can also run it to
verify connectivity outside of the MCP protocol:

```powershell
python _diag.py
```

## 4. Register with Claude Desktop

Open Claude Desktop's config file:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

If the file doesn't exist, create it. Add (or extend) the `mcpServers` block:

```json
{
  "mcpServers": {
    "cisco-imc": {
      "command": "C:\\mcp-servers\\cisco-imc-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\mcp-servers\\cisco-imc-mcp\\cisco_imc_mcp.py"],
      "cwd": "C:\\mcp-servers\\cisco-imc-mcp"
    }
  }
}
```

Adjust paths for your OS. On Windows, double-backslashes are required inside
JSON strings.

Fully quit Claude Desktop (tray icon → Quit) and reopen it. In a new chat
the IMC tools appear under the hammer/connector icon.

## First test in chat

Start with a read-only call:

> "Show me the Cisco server power status."

Claude calls `imc_get_power_status` and replies with something like
`Power state: on (BIOS POST: complete)`. If you get an authentication error,
recheck `.env`.

Then try:

> "Show me the system info."
>
> "What's the server health like? Any fan or PSU issues?"
>
> "List the physical drives."

Once you're confident the read-only tools work, you can try actions:

> "Reboot the Cisco server."

Claude will ask you to confirm. After you say yes, it calls `imc_reboot`
with `confirm=true`.

## Example workflows

**Pre-maintenance check:**

> "Give me a health report and power state of the Cisco server."

Claude calls `imc_get_health` and `imc_get_power_status`.

**Investigate the PSU 1 warning:**

> "Show me detailed PSU info, including input voltage."

Claude calls `imc_get_psu_details` and reports input V/A/W and the current
operability state of each PSU.

**Drill into a flaky drive:**

> "Show me the SMART counters for slot 3."

Claude calls `imc_get_disk_smart` with `slot_id="3"` and returns
`pdStatus`, `predictiveFailureCount`, `mediaErrorCount`, `otherErrorCount`
plus the full attribute dump.

**Check for ECC memory errors:**

> "Are any DIMMs degraded?"

Claude calls `imc_get_memory_health` and surfaces any slot whose
operability or operState isn't `operable`/`ok`.

**Investigate an intermittent fault:**

> "Show me the last 20 critical entries in the system event log."

Claude calls `imc_get_event_log` with `max_entries=20,
severity_filter="critical"`.

**Power cycle a stuck server:**

> "The Cisco server is hung. Power cycle it."

Claude calls `imc_power_cycle` with `confirm=true` after confirmation.

**Inventory drives:**

> "How many drives are in the Cisco box and what's their state?"

Claude calls `imc_list_drives`.

**Clear a near-full SEL (only after exporting):**

> "Export the SEL to me, then clear it."

Claude first calls `imc_get_event_log` with the full retention, then asks
for explicit `i_understand_data_loss=true` confirmation, then calls
`imc_clear_event_log`.

## Configuration reference

All settings come from environment variables, loaded from `.env`:

| Variable | Default | Description |
|---|---|---|
| `IMC_HOST` | — (required) | IP or hostname of the IMC web UI |
| `IMC_USERNAME` | — (required) | IMC username |
| `IMC_PASSWORD` | — (required) | IMC password |
| `IMC_VERIFY_SSL` | `false` | Verify the TLS certificate of the IMC |
| `IMC_TIMEOUT` | `30` | HTTP timeout in seconds |
| `IMC_RACK_DN` | `sys/rack-unit-1` | DN of the rack-unit to manage |

## How it works (the XML API)

This server uses Cisco's older but universally-supported **Nuova XML API**
(POST to `/nuova`), not Redfish. The reason: Redfish wasn't fully implemented
on older IMC firmware (especially HX/M4 hardware), while the XML API has
been stable since the very first IMC versions.

The session lifecycle for every tool call is:

1. `aaaLogin` — get a session cookie
2. one or more of `configResolveDn`, `configResolveClass`, `configConfMo`
3. `aaaLogout` — release the cookie (always, even on errors)

For power actions, the server writes `adminPower` on the
`computeRackUnit` object. The IMC then drives the operational state.

## Security notes

- The IMC password sits in `.env`. Restrict that file to your user account
  (`icacls` on Windows; `chmod 600` on Linux).
- Never expose the IMC to the internet. Keep it on a trusted LAN/VLAN, or
  behind a VPN.
- Use a dedicated IMC user where possible, not `admin`.
- Action tools require `confirm=true`. Don't remove that guard.
- `IMC_VERIFY_SSL=false` is the default because most IMCs ship with
  self-signed certs. If you've installed a trusted cert, set it to `true`.

## Troubleshooting

- **"Cannot connect to IMC at <host>"**
  Network problem. Ping the IMC. Confirm the web UI loads at
  `https://<host>/` from the same machine.

- **"IMC login returned no cookie (auth failed?)"**
  Username or password is wrong, or the user is locked. Check by logging
  in to the web UI with the same credentials.

- **"IMC API error <n>: <description>"**
  The XML API returned an error. The description usually says what's wrong.
  Common ones: permission denied (the user lacks the role), bad DN
  (`IMC_RACK_DN` is wrong for your chassis).

- **"Resource not found"** or empty results
  Very old IMC firmware may use different class IDs. The MOs this server
  reads — `computeRackUnit`, `equipmentPsu`, `equipmentPsuInputStats`,
  `equipmentPsuOutputStats`, `equipmentFan`, `processorEnvStats`,
  `storageLocalDisk`, `memoryArray`, `memoryUnit`, `sysdebugMEpLogEntry` —
  have been stable since IMC 2.x, but rare variants exist.

- **`imc_clear_event_log` returns an XML API error.**
  The exact class for the SEL clear action varies slightly between IMC
  versions. This server uses `sysdebugMEpLog` with `adminState="clear"`
  at DN `sys/rack-unit-1/mgmt/log-SEL`. If your IMC reports an error, the
  description usually names the right alternative (commonly
  `adminAction="clear-log"`).

- **Power state doesn't change immediately after an action.**
  Normal. Cisco hardware takes 30–90 seconds for power transitions to
  settle. Wait, then call `imc_get_power_status` again.

- **Tools don't appear in Claude Desktop.**
  Check `%APPDATA%\Claude\logs\mcp*.log` (Windows) for errors. The most
  common cause is a wrong path in `claude_desktop_config.json` or
  backslashes that weren't doubled.

## Project structure

```
cisco-imc-mcp/
├── cisco_imc_mcp.py    # The MCP server
├── _diag.py            # Optional connectivity diagnostic
├── requirements.txt    # Python dependencies
├── .env.example        # Template for your local .env
├── .gitignore
├── LICENSE             # GPL v3
├── README.md           # This file
└── README.tr.md        # Turkish version
```

## Contributing

Issues and PRs welcome. If you add a tool, please:

1. Follow the existing pattern: pydantic input model + `_require_config` +
   `_imc_session` context manager + error handling.
2. Tag destructive tools with `destructiveHint: True` in annotations and
   require `confirm=True` in the input model.
3. Update the tool list in this README.

## License

[GNU General Public License v3.0](./LICENSE) — see the `LICENSE` file for the
full text.
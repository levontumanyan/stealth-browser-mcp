# stealth-browser-mcp

Stealth browser automation for AI coding agents on macOS — an MCP server (`stealth-browser`), a transcript-driven audit CLI (`stealth-browser-audit`), and a Postgres telemetry shipper (`stealth-browser-shipper`).

Every tool in `bin/` is a self-contained PEP 723 `uv run --script` executable with auto-generated `--help`, zero global Python pollution, and instant startup.

# Why stealth-browser?

Launching a second browser binary alongside an active one on macOS can corrupt shared IPC rendezvous ports, freezing tab creation and IPC in your main browser. `stealth-browser` avoids this completely:

1. **CDP First**: Attempts to attach to an existing browser session over `http://127.0.0.1:9222` (`STEALTH_BROWSER_CDP_PORT`).
2. **Standalone Stealth Fallback**: Falls back to launching a dedicated Patchright stealth Chromium instance (`~/.agent-chromium-profile`) with native stealth masking and an isolated profile.
3. **Clean Lifecycle & Idle Watchdog**: Automatically shuts down the background Patchright Node driver after 5 minutes of inactivity (`STEALTH_BROWSER_IDLE_TIMEOUT=300`) and guarantees immediate subprocess teardown on `SIGTERM`, `SIGHUP`, or `stdin` EOF.

# Quickstart

```sh
git clone https://github.com/levontumanyan/stealth-browser-mcp.git ~/repos/stealth-browser-mcp
export PATH="$HOME/repos/stealth-browser-mcp/bin:$PATH"
stealth-browser --help
```

## MCP Configuration

Add to your agent's MCP configuration (`~/.gemini/config/mcp_config.json`, `~/.config/opencode/opencode.jsonc`, `~/.claude.json`, or `.cursor/mcp.json`):

```json
{
	"mcpServers": {
		"stealth-browser": {
			"command": "uv",
			"args": [
				"run",
				"--script",
				"/Users/your-username/repos/stealth-browser-mcp/bin/stealth-browser"
			]
		}
	}
}
```

Use the absolute path to your local checkout (avoid `~` in JSON).

## Available MCP Tools

- `status`: Check connection status and list all open tabs with indices and stable ids.
- `navigate`: Navigate the active tab or open a new tab (`new_tab=True`).
- `get_content` / `extract`: Extract visible text or structured fields from `body` or any CSS/XPath selector.
- `click` / `type` / `press_key` / `scroll`: Interact with page elements.
- `screenshot`: Capture a viewport screenshot for multimodal agent inspection.
- `evaluate_js`: Execute JavaScript in the active page context.
- `switch_tab` / `new_tab` / `close_tab`: Full multi-tab management (closing the last tab automatically closes the browser).
- `close_browser`: Completely close the browser window, terminate the browser process, and release all resources.

Headless by default; keep it that way and use `wait_for_human()` for interactive login/2FA/CAPTCHA.

# Audit & Telemetry

- `stealth-browser-audit [--since 14d] [--session <sid-prefix>] [--diff <baseline.json>]` — mine local AGY and OpenCode transcripts for stealth-browser usage, error rates, latency, and workarounds; produces a prioritized improvement plan or before/after scorecard.
- `stealth-browser-shipper [--once] [--full] [--dry-run]` — syncs audit records into `Antigravity_Postgres` (`mcp_audit_sessions` / `mcp_audit_calls`) every 5 minutes via the `com.levon.stealth-browser-audit-shipper` LaunchAgent, backing the Grafana "MCP Observability" dashboard.

# Tests

```sh
uv run pytest tests/ -x
```

# Agent Skills (`skills/`)

Reusable agent workflows live in [`skills/`](skills/), one directory per skill with a `SKILL.md`:

- `stealth-browser-audit` — mine local agent transcripts for stealth-browser usage, errors, latency, and workarounds.
- `stealth-browser-subagent` — delegate heavy browser flows to an isolated subagent so DOM output stays out of the parent context.

Register the directory with your harness instead of copying files:

- **OpenCode**: add `"~/repos/stealth-browser-mcp/skills"` to the `skills` array in your `opencode.jsonc`.
- **Google Antigravity (`agy`)**: add `{ "path": "~/repos/stealth-browser-mcp/skills" }` to the `entries` of `~/.gemini/config/skills.json`.

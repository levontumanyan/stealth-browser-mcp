# stealth-browser-mcp

[![PyPI](https://img.shields.io/pypi/v/stealthmcp)](https://pypi.org/project/stealthmcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![tests](https://github.com/levontumanyan/stealth-browser-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/levontumanyan/stealth-browser-mcp/actions/workflows/tests.yml)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![uv](https://img.shields.io/badge/powered%20by-uv-%231284da)

Stealth browser automation for AI coding agents on macOS — an MCP server (`stealth-browser`) and a transcript-driven audit CLI (`stealth-browser-audit`).

# Why stealth-browser?

Launching a second browser binary alongside an active one on macOS can corrupt shared IPC rendezvous ports, freezing tab creation and IPC in your main browser. `stealth-browser` avoids this completely:

1. **CDP First**: Attempts to attach to an existing browser session over `http://127.0.0.1:9222` (`STEALTH_BROWSER_CDP_PORT`).
2. **Standalone Stealth Fallback**: Falls back to launching a dedicated Patchright stealth Chromium instance (`~/.agent-chromium-profile`) with native stealth masking and an isolated profile.
3. **Clean Lifecycle & Idle Watchdog**: Automatically shuts down the background Patchright Node driver after 5 minutes of inactivity (`STEALTH_BROWSER_IDLE_TIMEOUT=300`) and guarantees immediate subprocess teardown on `SIGTERM`, `SIGHUP`, or `stdin` EOF.

# Quickstart

Review the [Trust model](#trust-model) for security and filesystem boundaries before exposing the server to untrusted prompts.

Prerequisites:

- **macOS** — the server uses `osascript` for human handoff and macOS-specific browser paths.
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — install with `brew install uv` or the official installer. uv manages Python and dependencies; no system Python setup is needed.
- A first-launch browser download (~195 MB network, ~370 MB on disk) via `stealth-browser --install-browser`.

Install the tool and register it with your agent — no clone and no absolute path required:

```sh
uv tool install stealthmcp
stealth-browser --install-browser
stealth-browser --doctor
```

Upgrade or remove later with `uv tool upgrade stealthmcp` / `uv tool uninstall stealthmcp`.

If `stealth-browser` is not found after install, run `uv tool update-shell` to add uv's tool directory (`~/.local/bin`) to your `PATH`. GUI-launched clients that ignore your shell `PATH` can use the full shim path printed by `uv tool dir` (for example `~/.local/bin/stealth-browser`).

## MCP Configuration

The install above provides a `stealth-browser` command on your `PATH`. The cleanest setup uses your client's CLI, which writes the config for you:

```sh
claude mcp add -s user stealth-browser -- stealth-browser
opencode mcp add --global stealth-browser -- stealth-browser
```

Or add the following to your agent's config (`~/.gemini/config/mcp_config.json`, `~/.config/opencode/opencode.jsonc`, `~/.claude.json`, or `.cursor/mcp.json`). No username-specific path needed:

```json
{
	"mcpServers": {
		"stealth-browser": {
			"command": "stealth-browser",
			"args": []
		}
	}
}
```

### Other install methods

- **No install (try it):** `uvx --from stealthmcp stealth-browser --doctor`, or run straight from the source without a checkout: `uv run https://raw.githubusercontent.com/levontumanyan/stealth-browser-mcp/main/bin/stealth_browser.py --doctor`.
- **pip (in a venv):** `python3 -m venv .venv && . .venv/bin/activate && pip install stealthmcp`, then the same `stealth-browser` command. Prefer a venv over a global `pip install`.
- **pipx:** `pipx install stealthmcp`, then the same `stealth-browser` command.
- **Existing checkout:** `uv run --script /absolute/path/to/stealth-browser-mcp/bin/stealth-browser` keeps working unchanged. Add that checkout's `bin/` to your `PATH` and run `stealth-browser --help` for a local dev setup.

## Verification

`stealth-browser --doctor` checks Python, dependency versions, cheat-sheet creation, and browser/CDP availability, then exits non-zero on failure. Add `--smoke` to actually launch Chromium and open a tab. `stealth-browser --version` prints the installed version.

## Shared Daemon & CLI

By default, `stealth-browser` runs a lightweight stdlib stdio proxy that auto-starts and shares a single local daemon over a `0600` Unix Domain Socket (scoped per CDP port and profile). Multiple MCP clients and subagents share one browser instance with per-client tab isolation.

- `stealth-browser --daemon-status` — print running daemon PID, socket/HTTP endpoints, active clients, and uptime.
- `stealth-browser --daemon-stop` — stop the running shared daemon cleanly.
- `stealth-browser --serve [--http [HOST:PORT]] [--daemon-idle-exit SECONDS]` — run the shared daemon directly; `--http` also enables loopback Streamable HTTP (default `127.0.0.1:9223`) with mandatory Bearer token auth; `--daemon-idle-exit` exits after idle seconds with 0 clients (`1800` default, `0` disables).
- `stealth-browser --standalone` — run an isolated in-process stdio MCP server instead of proxying to the shared daemon.

## Available MCP Tools

- `status`: Check connection status and list all open tabs with indices and stable ids.
- `navigate`: Navigate the active tab or open a new tab (`new_tab=True`).
- `get_content` / `extract`: Extract visible text or structured fields from `body` or any CSS/XPath selector.
- `click` / `type` / `press_key` / `scroll`: Interact with page elements.
- `screenshot`: Capture a viewport screenshot for multimodal agent inspection.
- `evaluate_js`: Execute JavaScript in the active page context.
- `switch_tab` / `new_tab` / `close_tab`: Full multi-tab management (closing the last tab automatically closes the browser).
- `close_browser`: Completely close the browser window, terminate the browser process, and release all resources.
- `wait_for_human`: Pause for interactive login/2FA/CAPTCHA with a macOS notification; pin follow-up calls to the tab's stable `tN` id.

Headless by default; keep it that way and use `wait_for_human()` for interactive login/2FA/CAPTCHA.

# Trust model

- **Transport & auth**: Default mode proxies stdio to a same-user `0600` Unix Domain Socket inside a `0700` runtime directory (`STEALTH_BROWSER_RUNTIME_DIR`, default `~/.cache/stealth-browser`). Optional `--serve --http [HOST:PORT]` binds loopback only, enforces a `Host` allowlist for DNS rebinding protection, and requires a `0600` Bearer token (`STEALTH_BROWSER_TOKEN_FILE`, default `~/.config/stealth-browser/daemon.token`). `--standalone` runs isolated in-process stdio with no socket or HTTP listener.
- **Security principal**: Every connected agent is the security principal and can perform any action the user can perform in the attached browser (including all open tabs when attaching over CDP `127.0.0.1:9222`). The shared daemon shares browser state across connected sessions and guards destructive lifecycle calls (`close_browser`, `restart_browser`, `set_headless`) against sibling sessions unless `force=True` is passed.
- **Filesystem boundaries**: `fetch(save_path=...)` and `screenshot(save_path=...)` write to any path writable by the OS user — treat paths originating from web content or untrusted prompts as untrusted. Browser downloads land in `STEALTH_BROWSER_DOWNLOADS_DIR` (`~/Downloads`), the profile lives in `STEALTH_BROWSER_PROFILE_DIR` (`~/.agent-chromium-profile`), and the optional Apple Passwords CRX extension is fetched from Google's CRX endpoint and verified before loading into the profile.
- **Network & credentials**: In-browser `fetch` sends `credentials: 'include'` with the current origin's cookies and `evaluate_js` reads arbitrary page state. Stealth masking reduces automation fingerprinting; it is not anonymity and does not isolate credentials.
- **Operator config & recovery**: `STEALTH_BROWSER_*` environment variables and MCP client configs are trusted host input; store secrets in macOS Keychain rather than configs or prompts. Recover from wedged or unwanted state via `close_browser(force=True)`, `restart_browser(force=True)`, `stealth-browser --daemon-stop`, or terminating the detached Chromium process; closing the last open tab with `close_tab` also closes the browser.
- **Explicit non-claims**: No guarantee against bot detection; no proxy or anonymity features; detection depends on site policy, IP reputation, and browser build.

# Audit

- `stealth-browser-audit [--since 14d] [--session <sid-prefix>] [--diff <baseline.json>]` — mine local AGY and OpenCode transcripts for stealth-browser usage, error rates, latency, and workarounds; produces a prioritized improvement plan or before/after scorecard. The audit CLI lives in the repo checkout (`bin/stealth-browser-audit`) and is not part of the installed `stealthmcp` tool.

# Development

```sh
git clone https://github.com/levontumanyan/stealth-browser-mcp.git
cd stealth-browser-mcp
uv sync
uv run pytest tests/ -x
```

`bin/stealth_browser.py` is the canonical source; `bin/stealth-browser` is a symlink that preserves the historical path and PEP 723 `uv run --script` workflow. Dependencies are declared in both the script's PEP 723 block and `pyproject.toml`, and a test keeps them in sync.

To publish a new version, see [RELEASING.md](RELEASING.md). Only a `v*` tag publishes; merging PRs does not.

# Agent Skills (`skills/`)

Reusable agent workflows live in [`skills/`](skills/), one directory per skill with a `SKILL.md`:

- `stealth-browser-audit` — mine local agent transcripts for stealth-browser usage, errors, latency, and workarounds.
- `stealth-browser-subagent` — delegate heavy browser flows to an isolated subagent so DOM output stays out of the parent context.

Register the directory with your harness instead of copying files:

- **OpenCode**: add `"~/repos/stealth-browser-mcp/skills"` to the `skills` array in your `opencode.jsonc`.
- **Google Antigravity (`agy`)**: add `{ "path": "~/repos/stealth-browser-mcp/skills" }` to the `entries` of `~/.gemini/config/skills.json`.

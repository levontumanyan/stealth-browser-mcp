# AGENTS.md

- **Layout**: `bin/stealth-browser` (MCP server + CLI), `bin/stealth-browser-audit` (transcript mining), `bin/stealth-browser-shipper` (Postgres sync). All are standalone PEP 723 `uv run --script` executables — edit them directly in `bin/`; there is no build step.
- **Tests**: Run with `uv run pytest tests/ -x`. Tests load the server module via `SourceFileLoader` from `bin/stealth-browser` (repo-root-relative), so no compilation or PATH setup is needed.
- **Skills**: `skills/<name>/SKILL.md` is the canonical, shareable location for workflows around these CLIs. Harnesses discover this directory via `~/.gemini/config/skills.json` (AGY) and the `skills` array in `opencode.jsonc` (OpenCode) — never copy skills elsewhere.
- **Shipper deployment**: Runs every 5 minutes via launchd `com.levon.stealth-browser-audit-shipper` (plist lives in the `home_directory` dotfiles repo) and ships audit records into `Antigravity_Postgres` (`mcp_audit_sessions` / `mcp_audit_calls`). It backs the Grafana "MCP Observability" dashboard; `antigravity-shipper` in the `agent-tools` repo handles conversation telemetry separately.
- **MCP registration points**: `~/.gemini/config/mcp_config.json` and the `mcp.servers` block of `~/.config/opencode/opencode.jsonc` (both symlinked from the `home_directory` dotfiles repo). Keep these pointing at the absolute local checkout path.
- **Style**: Indent with one tab per hierarchy level. Every CLI subcommand must appear in `--help`. Keep code self-explanatory with zero comments; place context and rationale in commit or PR bodies.

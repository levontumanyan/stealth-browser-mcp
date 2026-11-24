# AGENTS.md

- **Layout**: `bin/stealth-browser` (MCP server + CLI) and `bin/stealth-browser-audit` (transcript mining). All are standalone PEP 723 `uv run --script` executables — edit them directly in `bin/`; there is no build step.
- **Tests**: Run with `uv run pytest tests/ -x`. Tests load the server module via `SourceFileLoader` from `bin/stealth-browser` (repo-root-relative), so no compilation or PATH setup is needed.
- **Skills**: `skills/<name>/SKILL.md` is the canonical, shareable location for workflows around these CLIs. Harnesses discover this directory via `~/.gemini/config/skills.json` (AGY) and the `skills` array in `opencode.jsonc` (OpenCode) — never copy skills elsewhere.
- **Shipper deployment**: The Postgres shipper is personal infrastructure and lives outside this repo, in the private `home_directory` dotfiles repo (`scripts/stealth-browser-shipper` + the `com.levon.stealth-browser-audit-shipper` LaunchAgent). It reuses `bin/stealth-browser-audit` from this repo as its parsing source of truth.
- **MCP registration points**: `~/.gemini/config/mcp_config.json` and the `mcp.servers` block of `~/.config/opencode/opencode.jsonc` (both symlinked from the `home_directory` dotfiles repo). Keep these pointing at the absolute local checkout path.
- **Style**: Indent with one tab per hierarchy level. Every CLI subcommand must appear in `--help`. Keep code self-explanatory with zero comments; place context and rationale in commit or PR bodies.

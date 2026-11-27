# Releasing

Push a `v*` tag to release. Merging PRs or pushing to `main` only runs CI; nothing is published.

## Steps

1. Bump `__version__` in `bin/stealth_browser.py` (the source for `pyproject.toml` and the MCP server) and in `bin/stealth_browser_cli.py` (the `stealth-browser --version` output). Pick a version above the current PyPI release — PyPI versions are immutable.
2. Commit and push to `main`; wait for CI to pass.
3. Push a tag matching the version:

   ```sh
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   If `git push` hangs in a non-interactive shell, create the ref via the API:

   ```sh
   SHA=$(git rev-parse HEAD)
   curl -X POST https://api.github.com/repos/levontumanyan/stealth-browser-mcp/git/refs \
     -H "Authorization: Bearer $(gh auth token)" \
     -H "Accept: application/vnd.github+json" \
     -d "{\"ref\":\"refs/tags/vX.Y.Z\",\"sha\":\"$SHA\"}"
   ```

`.github/workflows/release.yml` then validates the tag against `__version__`, runs tests, builds and smoke-tests the wheel, publishes to PyPI via OIDC (environment `pypi`), and creates a GitHub Release with the wheel and sdist attached.

Verify at https://pypi.org/project/stealthmcp/ and https://github.com/levontumanyan/stealth-browser-mcp/releases, or with `uv tool install stealthmcp && stealth-browser --version`.

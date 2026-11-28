import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "stealth-browser"
MODULE = ROOT / "bin" / "stealth_browser.py"
AUDIT_SCRIPT = ROOT / "bin" / "stealth-browser-audit"
AUDIT_MODULE = ROOT / "bin" / "stealth_browser_audit.py"
CLI_MODULE = ROOT / "bin" / "stealth_browser_cli.py"
PYPROJECT = ROOT / "pyproject.toml"


def _script_text() -> str:
	return MODULE.read_text(encoding="utf-8")


def test_symlink_preserves_legacy_script_path():
	assert SCRIPT.is_symlink(), "bin/stealth-browser must remain a symlink for existing configs"
	assert SCRIPT.resolve() == MODULE.resolve()
	assert MODULE.stat().st_mode & 0o111
	assert AUDIT_SCRIPT.is_symlink(), "bin/stealth-browser-audit must remain a symlink for shipper & tests"
	assert AUDIT_SCRIPT.resolve() == AUDIT_MODULE.resolve()
	assert AUDIT_MODULE.stat().st_mode & 0o111


def test_pep723_metadata_matches_pyproject():
	script = _script_text()
	metadata = re.search(r"# /// script\n(.*?)\n# ///", script, re.DOTALL)
	assert metadata, "PEP 723 block missing"
	block = metadata.group(1)
	pyproject = PYPROJECT.read_text(encoding="utf-8")

	pins = dict(re.findall(r'"([A-Za-z0-9_-]+)==([^"]+)"', pyproject))
	assert pins, "no pinned dependencies found in pyproject.toml"
	for package, version in pins.items():
		pin = f"{package}=={version}"
		assert pin in block, f"{pin} missing from PEP 723 block"
		assert pin in pyproject, f"{pin} missing from pyproject.toml"
	assert "adblock" not in block
	assert "adblock" not in pyproject

	assert 'requires-python = ">=3.11"' in block
	assert 'requires-python = ">=3.11"' in pyproject
	assert 'name = "stealthmcp"' in pyproject
	assert 'stealth-browser = "stealth_browser_cli:main"' in pyproject
	assert 'stealth-browser-audit = "stealth_browser_audit:main"' in pyproject
	assert '"bin/stealth_browser_cli.py"' in pyproject
	assert '"bin/stealth_browser_audit.py"' in pyproject


def test_cli_version_exits_zero():
	result = subprocess.run(
		["uv", "run", "--script", str(SCRIPT), "--version"],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stderr
	assert "stealth-browser 0.1.1" in result.stdout


def test_cli_doctor_exits_zero():
	result = subprocess.run(
		["uv", "run", "--script", str(SCRIPT), "--doctor"],
		capture_output=True,
		text=True,
		timeout=120,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "All checks passed" in result.stdout

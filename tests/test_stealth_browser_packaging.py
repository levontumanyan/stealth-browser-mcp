import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "stealth-browser"
MODULE = ROOT / "bin" / "stealth_browser.py"
PYPROJECT = ROOT / "pyproject.toml"


def _script_text() -> str:
	return MODULE.read_text(encoding="utf-8")


def test_symlink_preserves_legacy_script_path():
	assert SCRIPT.is_symlink(), "bin/stealth-browser must remain a symlink for existing configs"
	assert SCRIPT.resolve() == MODULE.resolve()
	assert MODULE.stat().st_mode & 0o111


def test_pep723_metadata_matches_pyproject():
	script = _script_text()
	metadata = re.search(r"# /// script\n(.*?)\n# ///", script, re.DOTALL)
	assert metadata, "PEP 723 block missing"
	block = metadata.group(1)
	pyproject = PYPROJECT.read_text(encoding="utf-8")

	for package in ("adblock==0.6.0", "mcp==1.30.0", "patchright==1.64.4"):
		assert package in block, f"{package} missing from PEP 723 block"
		assert package in pyproject, f"{package} missing from pyproject.toml"

	assert 'requires-python = ">=3.11"' in block
	assert 'requires-python = ">=3.11"' in pyproject
	assert 'name = "stealthmcp"' in pyproject
	assert 'stealth-browser = "stealth_browser:main"' in pyproject


def test_version_is_single_source():
	script = _script_text()
	version = re.search(r'^__version__ = "([^"]+)"', script, re.MULTILINE)
	assert version, "__version__ missing"
	assert version.group(1) == "0.1.0"
	assert "__version__" not in PYPROJECT.read_text(encoding="utf-8"), "version must come from the script only"


def test_cli_flags_are_exposed():
	result = subprocess.run(
		["uv", "run", "--script", str(SCRIPT), "--help"],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stderr
	for flag in ("--version", "--install-browser", "--doctor", "--smoke"):
		assert flag in result.stdout, f"{flag} missing from --help"


def test_cli_version_exits_zero():
	result = subprocess.run(
		["uv", "run", "--script", str(SCRIPT), "--version"],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stderr
	assert "stealth-browser 0.1.0" in result.stdout


def test_cli_doctor_exits_zero():
	result = subprocess.run(
		["uv", "run", "--script", str(SCRIPT), "--doctor"],
		capture_output=True,
		text=True,
		timeout=120,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "All checks passed" in result.stdout

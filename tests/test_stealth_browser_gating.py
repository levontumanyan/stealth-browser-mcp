import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "stealth-browser"
CLI_SCRIPT = ROOT / "bin" / "stealth_browser_cli.py"


def _load_stealth_browser():
	loader = SourceFileLoader("stealth_browser_gating_test", str(SCRIPT))
	spec = spec_from_loader("stealth_browser_gating_test", loader)
	module = module_from_spec(spec)
	loader.exec_module(module)
	return module


sb = _load_stealth_browser()


def _short_runtime_dir():
	return tempfile.TemporaryDirectory(prefix="sb_gate_", dir="/tmp")


def _isolated_env(rdir: str, cdp_port: int) -> dict[str, str]:
	env = os.environ.copy()
	env.pop("STEALTH_BROWSER_MINIMAL", None)
	env.pop("STEALTH_BROWSER_DISABLE_SECTIONS", None)
	env["STEALTH_BROWSER_RUNTIME_DIR"] = rdir
	env["STEALTH_BROWSER_TOKEN_FILE"] = os.path.join(rdir, "daemon.token")
	env["STEALTH_BROWSER_CDP_PORT"] = str(cdp_port)
	env["STEALTH_BROWSER_PROFILE_DIR"] = os.path.join(rdir, "prof")
	return env


def _mcp_exchange(cmd: list[str], messages: list[dict], env: dict[str, str], timeout: float = 20.0) -> dict[int, dict]:
	proc = subprocess.Popen(
		cmd,
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		env=env,
	)
	out: dict[int, dict] = {}
	try:
		for msg in messages:
			proc.stdin.write(json.dumps(msg) + "\n")
			proc.stdin.flush()
			if "id" in msg:
				line = proc.stdout.readline()
				assert line, f"EOF waiting for id={msg['id']}; stderr={proc.stderr.read()}"
				obj = json.loads(line)
				if isinstance(obj, dict) and "id" in obj:
					out[obj["id"]] = obj
		proc.stdin.close()
		proc.wait(timeout=timeout)
		assert proc.returncode == 0, proc.stderr.read()
	finally:
		if proc.poll() is None:
			proc.terminate()
			proc.wait(timeout=5.0)
	return out


def _init_messages() -> list[dict]:
	return [
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "initialize",
			"params": {
				"protocolVersion": "2025-03-26",
				"capabilities": {},
				"clientInfo": {"name": "gating-test", "version": "1.0"},
			},
		},
		{"jsonrpc": "2.0", "method": "notifications/initialized"},
	]


def test_section_map_is_exact_partition_of_registered_tools():
	registered = {t.name for t in asyncio.run(sb.mcp.list_tools())}
	assert len(registered) == 28

	assert len(sb.TOOL_SECTIONS["core"]) == 16
	assert len(sb.TOOL_SECTIONS["debug"]) == 2
	assert len(sb.TOOL_SECTIONS["network"]) == 4
	assert len(sb.TOOL_SECTIONS["lifecycle"]) == 6

	seen: list[str] = []
	for tools in sb.TOOL_SECTIONS.values():
		seen.extend(tools)
	assert len(seen) == len(set(seen)), "Duplicate tool in TOOL_SECTIONS"
	assert set(seen) == registered
	assert sb.PROTECTED_TOOLS == frozenset({"status", "wait_for_human"})
	assert sb.PROTECTED_TOOLS.issubset(set(sb.TOOL_SECTIONS["core"]))


def test_resolve_disabled_tools_env_and_protected_guards():
	disabled, sections, minimal = sb._resolve_disabled_tools(env={})
	assert disabled == frozenset()
	assert sections == ()
	assert minimal is False

	disabled, sections, minimal = sb._resolve_disabled_tools(minimal=True, env={})
	assert minimal is True
	assert set(sections) == {"debug", "network", "lifecycle"}
	assert len(disabled) == 12
	assert "status" not in disabled and "wait_for_human" not in disabled
	assert "evaluate_js" in disabled and "close_browser" in disabled and "fetch" in disabled

	disabled_env, sections_env, minimal_env = sb._resolve_disabled_tools(
		env={"STEALTH_BROWSER_MINIMAL": "1"},
	)
	assert minimal_env is True
	assert disabled_env == disabled
	assert sections_env == sections

	disabled_all, _, _ = sb._resolve_disabled_tools(
		disabled_sections=["core", "debug", "network", "lifecycle"],
		env={},
	)
	assert len(disabled_all) == 26
	assert "status" not in disabled_all
	assert "wait_for_human" not in disabled_all

	disabled_csv, sections_csv, _ = sb._resolve_disabled_tools(
		env={"STEALTH_BROWSER_DISABLE_SECTIONS": "debug, network"},
	)
	assert sections_csv == ("debug", "network")
	assert disabled_csv == frozenset(sb.TOOL_SECTIONS["debug"] + sb.TOOL_SECTIONS["network"])

	with pytest.raises(ValueError, match="Unknown tool section"):
		sb._resolve_disabled_tools(disabled_sections=["nonexistent"], env={})


def test_shared_daemon_per_client_gating_in_stdio_proxy():
	with _short_runtime_dir() as td:
		base_env = _isolated_env(td, cdp_port=19241)
		min_env = dict(base_env, STEALTH_BROWSER_MINIMAL="1")
		dbg_env = dict(base_env, STEALTH_BROWSER_DISABLE_SECTIONS="debug")
		msgs = _init_messages() + [
			{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
			{
				"jsonrpc": "2.0",
				"id": 3,
				"method": "tools/call",
				"params": {"name": "evaluate_js", "arguments": {"expression": "1+1"}},
			},
		]
		try:
			by_id_min = _mcp_exchange([sys.executable, str(CLI_SCRIPT)], msgs, min_env)
			assert "[Tool gating active: --minimal (16 core tools enabled)" in by_id_min[1]["result"]["instructions"]
			min_tools = {t["name"] for t in by_id_min[2]["result"]["tools"]}
			assert min_tools == set(sb.TOOL_SECTIONS["core"])
			assert by_id_min[3]["error"]["code"] == -32602
			assert "disabled in this session" in by_id_min[3]["error"]["message"]

			by_id_full = _mcp_exchange(
				[sys.executable, str(CLI_SCRIPT)],
				_init_messages() + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}],
				base_env,
			)
			full_tools = {t["name"] for t in by_id_full[2]["result"]["tools"]}
			assert len(full_tools) == 28
			assert "[Tool gating active:" not in by_id_full[1]["result"]["instructions"]

			by_id_dbg = _mcp_exchange([sys.executable, str(CLI_SCRIPT)], msgs, dbg_env)
			dbg_tools = {t["name"] for t in by_id_dbg[2]["result"]["tools"]}
			assert len(dbg_tools) == 26
			assert "evaluate_js" not in dbg_tools and "state" not in dbg_tools
			assert by_id_dbg[3]["error"]["code"] == -32602
		finally:
			subprocess.run(
				[sys.executable, str(CLI_SCRIPT), "--daemon-stop"],
				capture_output=True,
				env=base_env,
				timeout=10,
			)


def test_standalone_and_serve_modes_remove_disabled_tools():
	with _short_runtime_dir() as td:
		env = _isolated_env(td, cdp_port=19242)
		msgs = _init_messages() + [
			{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
			{
				"jsonrpc": "2.0",
				"id": 3,
				"method": "tools/call",
				"params": {"name": "evaluate_js", "arguments": {"expression": "1+1"}},
			},
		]
		by_id = _mcp_exchange([sys.executable, str(CLI_SCRIPT), "--standalone", "--minimal"], msgs, env)
		assert "[Tool gating active: --minimal (16 core tools enabled)" in by_id[1]["result"]["instructions"]
		tools = {t["name"] for t in by_id[2]["result"]["tools"]}
		assert tools == set(sb.TOOL_SECTIONS["core"])
		assert "error" in by_id[3] or by_id[3].get("result", {}).get("isError") is True

		by_id_dbg = _mcp_exchange(
			[sys.executable, str(CLI_SCRIPT), "--standalone", "--disable-debug"],
			_init_messages() + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}],
			env,
		)
		dbg_tools = {t["name"] for t in by_id_dbg[2]["result"]["tools"]}
		assert len(dbg_tools) == 26
		assert "evaluate_js" not in dbg_tools and "state" not in dbg_tools

		paths = sb._daemon_paths(cdp_port=19242, profile_dir=env["STEALTH_BROWSER_PROFILE_DIR"], runtime_dir=td)
		proc = subprocess.Popen(
			[sys.executable, str(CLI_SCRIPT), "--serve", "--disable-debug", "--daemon-idle-exit", "0"],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=env,
		)
		try:
			deadline = time.monotonic() + 10.0
			while time.monotonic() < deadline:
				assert proc.poll() is None, proc.stderr.read().decode()
				if sb._probe_daemon_health(paths["sock_path"], timeout=0.3) is not None:
					break
				time.sleep(0.05)

			by_id_serve = _mcp_exchange(
				[sys.executable, str(CLI_SCRIPT)],
				_init_messages() + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}],
				env,
			)
			serve_tools = {t["name"] for t in by_id_serve[2]["result"]["tools"]}
			assert len(serve_tools) == 26
			assert "evaluate_js" not in serve_tools and "state" not in serve_tools
		finally:
			subprocess.run(
				[sys.executable, str(CLI_SCRIPT), "--daemon-stop"],
				capture_output=True,
				env=env,
				timeout=10,
			)
			if proc.poll() is None:
				proc.terminate()
				proc.wait(timeout=5.0)

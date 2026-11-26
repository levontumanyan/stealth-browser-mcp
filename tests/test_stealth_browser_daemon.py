import http.client
import json
import os
import socket
import stat
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


def _load_stealth_browser():
	loader = SourceFileLoader("stealth_browser_daemon_test", str(SCRIPT))
	spec = spec_from_loader("stealth_browser_daemon_test", loader)
	module = module_from_spec(spec)
	loader.exec_module(module)
	return module


sb = _load_stealth_browser()


def _free_loopback_port() -> int:
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


def _short_runtime_dir():
	return tempfile.TemporaryDirectory(prefix="sb_test_", dir="/tmp")


def _isolated_env(rdir: str, token_file: str, cdp_port: int = 19222) -> dict[str, str]:
	env = os.environ.copy()
	env["STEALTH_BROWSER_RUNTIME_DIR"] = rdir
	env["STEALTH_BROWSER_TOKEN_FILE"] = token_file
	env["STEALTH_BROWSER_CDP_PORT"] = str(cdp_port)
	env["STEALTH_BROWSER_PROFILE_DIR"] = os.path.join(rdir, "prof")
	return env


def test_help_exposes_all_daemon_and_proxy_flags():
	res = subprocess.run(
		[sys.executable, str(SCRIPT), "--help"],
		capture_output=True,
		text=True,
		timeout=15,
	)
	assert res.returncode == 0, res.stderr
	for flag in (
		"--version",
		"--install-browser",
		"--doctor",
		"--smoke",
		"--serve",
		"--http",
		"--daemon-idle-exit",
		"--daemon-status",
		"--daemon-stop",
		"--standalone",
	):
		assert flag in res.stdout, f"{flag} missing from --help"


def test_parse_loopback_http_enforces_loopback_only():
	assert sb._parse_loopback_http("127.0.0.1:9223") == ("127.0.0.1", 9223)
	assert sb._parse_loopback_http("localhost:9224") == ("localhost", 9224)
	assert sb._parse_loopback_http("[::1]:9225") == ("::1", 9225)

	for bad in ("0.0.0.0:9223", "192.168.1.10:9223", "example.com:9223", "127.0.0.1", "127.0.0.1:99999", ""):
		with pytest.raises(ValueError):
			sb._parse_loopback_http(bad)


def test_ensure_daemon_token_creates_0600_file_and_0700_dir():
	with _short_runtime_dir() as td:
		tdir = os.path.join(td, "cfg")
		tpath = os.path.join(tdir, "daemon.token")
		tok1 = sb._ensure_daemon_token(tpath)
		assert len(tok1) >= 32
		assert stat.S_IMODE(os.stat(tdir).st_mode) == 0o700
		assert stat.S_IMODE(os.stat(tpath).st_mode) == 0o600
		tok2 = sb._ensure_daemon_token(tpath)
		assert tok2 == tok1


def test_fast_path_skips_heavy_imports():
	with _short_runtime_dir() as td:
		probe_code = (
			"import runpy, sys\n"
			f"sys.argv = [{str(SCRIPT)!r}, '--daemon-status']\n"
			"try:\n"
			f"    runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
			"except SystemExit:\n"
			"    pass\n"
			"loaded = [m for m in ('mcp', 'patchright', 'adblock') if m in sys.modules]\n"
			"print('LOADED:' + ','.join(loaded))\n"
		)
		env = _isolated_env(td, os.path.join(td, "tok"))
		res = subprocess.run(
			[sys.executable, "-c", probe_code],
			capture_output=True,
			text=True,
			env=env,
			timeout=15,
		)
		assert res.returncode == 0, res.stderr
		assert "LOADED:\n" in res.stdout or res.stdout.rstrip().endswith("LOADED:")


def test_uds_daemon_permissions_status_stop_and_idle_exit():
	with _short_runtime_dir() as td:
		env = _isolated_env(td, os.path.join(td, "tok"), cdp_port=19223)
		paths = sb._daemon_paths(cdp_port=19223, profile_dir=env["STEALTH_BROWSER_PROFILE_DIR"], runtime_dir=td)
		proc = subprocess.Popen(
			[sys.executable, str(SCRIPT), "--serve", "--daemon-idle-exit", "0"],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=env,
		)
		try:
			deadline = time.monotonic() + 10.0
			health = None
			while time.monotonic() < deadline:
				assert proc.poll() is None, f"Daemon exited early: {proc.stderr.read().decode()}"
				health = sb._probe_daemon_health(paths["sock_path"], timeout=0.3)
				if health is not None:
					break
				time.sleep(0.05)
			assert health is not None
			assert health["pid"] == proc.pid
			assert stat.S_IMODE(os.stat(paths["sock_path"]).st_mode) == 0o600
			assert stat.S_IMODE(os.stat(td).st_mode) == 0o700

			st_res = subprocess.run(
				[sys.executable, str(SCRIPT), "--daemon-status"],
				capture_output=True,
				text=True,
				env=env,
				timeout=10,
			)
			assert st_res.returncode == 0
			assert "Daemon: running" in st_res.stdout
			assert "mode=0o600" in st_res.stdout

			stop_res = subprocess.run(
				[sys.executable, str(SCRIPT), "--daemon-stop"],
				capture_output=True,
				text=True,
				env=env,
				timeout=10,
			)
			assert stop_res.returncode == 0
			assert "Daemon: stopped" in stop_res.stdout
			proc.wait(timeout=5.0)
			assert not os.path.exists(paths["sock_path"])

			st_after = subprocess.run(
				[sys.executable, str(SCRIPT), "--daemon-status"],
				capture_output=True,
				text=True,
				env=env,
				timeout=10,
			)
			assert st_after.returncode == 1
			assert "Daemon: stopped" in st_after.stdout
		finally:
			if proc.poll() is None:
				proc.terminate()
				proc.wait(timeout=5.0)


def test_stdio_proxy_autostarts_daemon_and_roundtrips_jsonrpc():
	with _short_runtime_dir() as td:
		env = _isolated_env(td, os.path.join(td, "tok"), cdp_port=19224)
		paths = sb._daemon_paths(cdp_port=19224, profile_dir=env["STEALTH_BROWSER_PROFILE_DIR"], runtime_dir=td)
		assert not os.path.exists(paths["sock_path"])

		init_msg = json.dumps({
			"jsonrpc": "2.0",
			"id": 1,
			"method": "initialize",
			"params": {
				"protocolVersion": "2025-03-26",
				"capabilities": {},
				"clientInfo": {"name": "proxy-test", "version": "1.0"},
			},
		})
		initialized_msg = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
		list_msg = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
		stdin_payload = f"{init_msg}\n{initialized_msg}\n{list_msg}\n"

		try:
			res = subprocess.run(
				[sys.executable, str(SCRIPT)],
				input=stdin_payload,
				capture_output=True,
				text=True,
				env=env,
				timeout=20,
			)
			assert res.returncode == 0, res.stderr
			lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
			by_id = {m.get("id"): m for m in lines if isinstance(m, dict) and "id" in m}
			assert 1 in by_id and "result" in by_id[1]
			assert by_id[1]["result"]["serverInfo"]["name"] == "stealth-browser"
			assert 2 in by_id and "result" in by_id[2]
			tool_names = {t["name"] for t in by_id[2]["result"]["tools"]}
			assert "status" in tool_names and "navigate" in tool_names

			health = sb._probe_daemon_health(paths["sock_path"], timeout=1.0)
			assert health is not None
			assert health["active_sessions"] == 0
		finally:
			subprocess.run(
				[sys.executable, str(SCRIPT), "--daemon-stop"],
				capture_output=True,
				env=env,
				timeout=10,
			)


def test_http_loopback_requires_bearer_token_and_accepts_valid_token():
	with _short_runtime_dir() as td:
		token_path = os.path.join(td, "cfg", "daemon.token")
		port = _free_loopback_port()
		env = _isolated_env(td, token_path, cdp_port=19225)
		paths = sb._daemon_paths(cdp_port=19225, profile_dir=env["STEALTH_BROWSER_PROFILE_DIR"], runtime_dir=td)

		bad_res = subprocess.run(
			[sys.executable, str(SCRIPT), "--serve", "--http", f"0.0.0.0:{port}"],
			capture_output=True,
			text=True,
			env=env,
			timeout=15,
		)
		assert bad_res.returncode == 2
		assert "non-loopback" in bad_res.stderr

		proc = subprocess.Popen(
			[sys.executable, str(SCRIPT), "--serve", "--http", f"127.0.0.1:{port}", "--daemon-idle-exit", "0"],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=env,
		)
		try:
			deadline = time.monotonic() + 10.0
			while time.monotonic() < deadline:
				assert proc.poll() is None, f"Daemon exited early: {proc.stderr.read().decode()}"
				if sb._probe_daemon_health(paths["sock_path"], timeout=0.3) is not None:
					break
				time.sleep(0.05)

			assert os.path.isfile(token_path)
			assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600
			token = Path(token_path).read_text(encoding="utf-8").strip()

			init_body = json.dumps({
				"jsonrpc": "2.0",
				"id": 1,
				"method": "initialize",
				"params": {
					"protocolVersion": "2025-03-26",
					"capabilities": {},
					"clientInfo": {"name": "http-test", "version": "1.0"},
				},
			}).encode("utf-8")

			conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
			conn.request("POST", "/mcp", body=init_body, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
			r_noauth = conn.getresponse()
			r_noauth.read()
			assert r_noauth.status == 401
			assert r_noauth.getheader("www-authenticate") == "Bearer"
			conn.close()

			conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
			conn.request(
				"POST",
				"/mcp",
				body=init_body,
				headers={
					"Content-Type": "application/json",
					"Accept": "application/json, text/event-stream",
					"Authorization": "Bearer wrong-token",
				},
			)
			r_bad = conn.getresponse()
			r_bad.read()
			assert r_bad.status == 401
			conn.close()

			conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
			conn.request(
				"POST",
				"/mcp",
				body=init_body,
				headers={
					"Content-Type": "application/json",
					"Accept": "application/json, text/event-stream",
					"Authorization": f"Bearer {token}",
				},
			)
			r_ok = conn.getresponse()
			ok_data = r_ok.read()
			assert r_ok.status == 200, ok_data.decode("utf-8", errors="replace")
			assert r_ok.getheader("mcp-session-id")
			conn.close()
		finally:
			subprocess.run(
				[sys.executable, str(SCRIPT), "--daemon-stop"],
				capture_output=True,
				env=env,
				timeout=10,
			)
			if proc.poll() is None:
				proc.terminate()
				proc.wait(timeout=5.0)


def test_standalone_mode_runs_in_process_without_spawning_daemon():
	with _short_runtime_dir() as td:
		env = _isolated_env(td, os.path.join(td, "tok"), cdp_port=19226)
		paths = sb._daemon_paths(cdp_port=19226, profile_dir=env["STEALTH_BROWSER_PROFILE_DIR"], runtime_dir=td)
		init_msg = json.dumps({
			"jsonrpc": "2.0",
			"id": 1,
			"method": "initialize",
			"params": {
				"protocolVersion": "2025-03-26",
				"capabilities": {},
				"clientInfo": {"name": "standalone-test", "version": "1.0"},
			},
		})
		res = subprocess.run(
			[sys.executable, str(SCRIPT), "--standalone"],
			input=f"{init_msg}\n",
			capture_output=True,
			text=True,
			env=env,
			timeout=15,
		)
		assert res.returncode == 0, res.stderr
		lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
		assert any(m.get("id") == 1 and "result" in m for m in lines)
		assert not os.path.exists(paths["sock_path"])


def test_cli_shim_fast_path_skips_heavy_imports_and_heavy_flags_delegate():
	cli_script = ROOT / "bin" / "stealth_browser_cli.py"
	with _short_runtime_dir() as td:
		probe_code = (
			"import runpy, sys\n"
			f"sys.argv = [{str(cli_script)!r}, '--daemon-status']\n"
			"try:\n"
			f"    runpy.run_path({str(cli_script)!r}, run_name='__main__')\n"
			"except SystemExit:\n"
			"    pass\n"
			"loaded = [m for m in ('stealth_browser', 'mcp', 'patchright', 'adblock') if m in sys.modules]\n"
			"print('LOADED:' + ','.join(loaded))\n"
		)
		env = _isolated_env(td, os.path.join(td, "tok"))
		res = subprocess.run(
			[sys.executable, "-c", probe_code],
			capture_output=True,
			text=True,
			env=env,
			timeout=15,
		)
		assert res.returncode == 0, res.stderr
		assert res.stdout.rstrip().endswith("LOADED:")

		help_res = subprocess.run(
			[sys.executable, str(cli_script), "--help"],
			capture_output=True,
			text=True,
			timeout=15,
		)
		assert help_res.returncode == 0
		assert "--serve" in help_res.stdout and "--standalone" in help_res.stdout


def test_daemon_paths_af_unix_length_fallback_and_secure_dir():
	long_rdir = "/tmp/" + ("a" * 95)
	paths = sb._daemon_paths(cdp_port=19227, profile_dir="/tmp/prof", runtime_dir=long_rdir)
	assert len(paths["sock_path"].encode("utf-8")) <= 100
	assert paths["runtime_dir"] == f"/tmp/sb-{os.getuid()}"

	other_long_rdir = "/tmp/" + ("b" * 95)
	paths2 = sb._daemon_paths(cdp_port=19227, profile_dir="/tmp/prof", runtime_dir=other_long_rdir)
	assert paths2["sock_path"] != paths["sock_path"]

	with _short_runtime_dir() as td:
		target = os.path.join(td, "real")
		os.makedirs(target, mode=0o700)
		link = os.path.join(td, "link")
		os.symlink(target, link)
		with pytest.raises(PermissionError):
			sb._ensure_secure_runtime_dir(link)


def test_proxy_dynamic_timeout_for_wait_for_human():
	proxy = sb._StdioUdsProxy("/tmp/nonexistent.sock", request_timeout=300.0)
	assert proxy._timeout_for_request("tools/list", {"method": "tools/list"}) == 300.0
	assert proxy._timeout_for_request(
		"tools/call",
		{"method": "tools/call", "params": {"name": "navigate", "arguments": {"url": "https://example.com"}}},
	) == 300.0
	assert proxy._timeout_for_request(
		"tools/call",
		{"method": "tools/call", "params": {"name": "wait_for_human", "arguments": {"timeout_s": 180}}},
	) == 300.0
	assert proxy._timeout_for_request(
		"tools/call",
		{"method": "tools/call", "params": {"name": "wait_for_human", "arguments": {"timeout_s": 600}}},
	) == 630.0

#!/usr/bin/env python3
"""Lightweight stdlib-only CLI entrypoint and stdio-to-UDS proxy for stealth-browser."""

__version__ = "0.1.1"

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Optional

CDP_PORT = int(os.environ.get("STEALTH_BROWSER_CDP_PORT", os.environ.get("CDP_PORT", "9222")))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = os.path.expanduser(os.environ.get("STEALTH_BROWSER_PROFILE_DIR", "~/.agent-chromium-profile"))
DAEMON_DEFAULT_HTTP = "127.0.0.1:9223"
DAEMON_DEFAULT_IDLE_EXIT = 1800
_HEAVY_CLI_FLAGS = frozenset({"--serve", "--standalone", "--install-browser", "--doctor", "--smoke"})
_AF_UNIX_MAX_SAFE_BYTES = 100

PROTECTED_TOOLS = frozenset({"status", "wait_for_human"})
TOOL_SECTIONS: dict[str, tuple[str, ...]] = {
	"core": (
		"status",
		"wait_for_human",
		"new_tab",
		"navigate",
		"switch_tab",
		"close_tab",
		"snapshot",
		"get_content",
		"extract",
		"click",
		"type",
		"press_key",
		"scroll",
		"wait_for",
		"screenshot",
		"batch",
	),
	"debug": ("evaluate_js", "state"),
	"network": ("fetch", "downloads", "network_requests", "console_logs"),
	"lifecycle": (
		"close_browser",
		"restart_browser",
		"set_headless",
		"set_media",
		"set_animations",
		"keep_alive",
	),
}
_DISABLEABLE_SECTIONS = ("debug", "network", "lifecycle")


def _resolve_disabled_tools(
	minimal: bool = False,
	disabled_sections: Optional[list[str]] = None,
	env: Optional[dict[str, str]] = None,
) -> tuple[frozenset[str], tuple[str, ...], bool]:
	src_env = os.environ if env is None else env
	env_minimal = src_env.get("STEALTH_BROWSER_MINIMAL", "").strip().lower() in ("1", "true", "yes")
	effective_minimal = bool(minimal or env_minimal)
	requested: list[str] = []
	if effective_minimal:
		requested.extend(_DISABLEABLE_SECTIONS)
	if disabled_sections:
		requested.extend(disabled_sections)
	raw_env_sections = src_env.get("STEALTH_BROWSER_DISABLE_SECTIONS", "")
	if raw_env_sections.strip():
		for part in raw_env_sections.split(","):
			sec = part.strip().lower()
			if sec:
				requested.append(sec)
	ordered: list[str] = []
	seen: set[str] = set()
	for sec in requested:
		key = sec.strip().lower()
		if not key:
			continue
		if key not in TOOL_SECTIONS:
			valid = ", ".join(TOOL_SECTIONS.keys())
			raise ValueError(f"Unknown tool section {sec!r}; valid sections: {valid}")
		if key not in seen:
			seen.add(key)
			ordered.append(key)
	disabled: set[str] = set()
	for sec in ordered:
		disabled.update(TOOL_SECTIONS[sec])
	disabled.difference_update(PROTECTED_TOOLS)
	return frozenset(disabled), tuple(ordered), effective_minimal


def _gating_notice(minimal: bool, disabled_sections: tuple[str, ...], disabled_tools: frozenset[str]) -> str:
	if not disabled_tools:
		return ""
	total = sum(len(v) for v in TOOL_SECTIONS.values())
	enabled = total - len(disabled_tools)
	if minimal and set(disabled_sections) == set(_DISABLEABLE_SECTIONS):
		return f"[Tool gating active: --minimal ({enabled} core tools enabled); disabled sections: {', '.join(disabled_sections)}]"
	return f"[Tool gating active: {enabled}/{total} tools enabled; disabled sections: {', '.join(disabled_sections)}]"


def _format_sections_list() -> str:
	total = sum(len(v) for v in TOOL_SECTIONS.values())
	lines = [f"stealth-browser tool sections ({total} tools total; protected in all modes: {', '.join(sorted(PROTECTED_TOOLS))}):"]
	for name, tools in TOOL_SECTIONS.items():
		lines.append(f"  {name} ({len(tools)}): {', '.join(tools)}")
	return "\n".join(lines)


def _disabled_sections_from_args(args: argparse.Namespace) -> list[str]:
	out: list[str] = []
	for sec in _DISABLEABLE_SECTIONS:
		if getattr(args, f"disable_{sec}", False):
			out.append(sec)
	if getattr(args, "disable_core", False):
		out.append("core")
	return out


def _build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog="stealth-browser",
		description="FastMCP server providing browser automation tools over CDP or standalone stealth Chromium using patchright.",
	)
	parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
	parser.add_argument("--install-browser", action="store_true", help="Download the Patchright Chromium build used by the stealth browser")
	parser.add_argument("--doctor", action="store_true", help="Check installation health and verify the MCP server can start")
	parser.add_argument("--smoke", action="store_true", help="With --doctor, launch the browser and open a tab")
	parser.add_argument("--serve", action="store_true", help="Run shared daemon on a 0600 Unix Domain Socket (auto-started by default stdio proxy)")
	parser.add_argument(
		"--http",
		nargs="?",
		const=DAEMON_DEFAULT_HTTP,
		default=None,
		metavar="HOST:PORT",
		help=f"With --serve, also expose loopback Streamable HTTP with mandatory Bearer token auth (default: {DAEMON_DEFAULT_HTTP})",
	)
	parser.add_argument(
		"--daemon-idle-exit",
		type=int,
		default=DAEMON_DEFAULT_IDLE_EXIT,
		metavar="SECONDS",
		help=f"With --serve, exit daemon after SECONDS idle with 0 active clients (0 disables; default: {DAEMON_DEFAULT_IDLE_EXIT})",
	)
	parser.add_argument("--daemon-status", action="store_true", help="Print shared daemon status and exit")
	parser.add_argument("--daemon-stop", action="store_true", help="Stop the running shared daemon cleanly and exit")
	parser.add_argument(
		"--kill-all",
		action="store_true",
		help="Stop the daemon, kill all browser processes for this profile, and clear stale locks/state",
	)
	parser.add_argument("--standalone", action="store_true", help="Run direct in-process stdio MCP server instead of proxying to the shared daemon")
	parser.add_argument("--list-sections", action="store_true", help="Print tool section names, counts, and membership and exit")
	parser.add_argument("--minimal", action="store_true", help="Expose only the 16 core interaction tools (also via STEALTH_BROWSER_MINIMAL=1)")
	parser.add_argument("--disable-debug", action="store_true", help="Disable debug tools (evaluate_js, state)")
	parser.add_argument("--disable-network", action="store_true", help="Disable network tools (fetch, downloads, network_requests, console_logs)")
	parser.add_argument("--disable-lifecycle", action="store_true", help="Disable lifecycle tools (close_browser, restart_browser, set_headless, set_media, set_animations, keep_alive)")
	parser.add_argument("--disable-core", action="store_true", help="Disable non-protected core tools (status and wait_for_human always remain enabled)")
	return parser


def _server_script_path() -> str:
	return os.path.join(os.path.dirname(os.path.realpath(__file__)), "stealth_browser.py")


def _ensure_secure_runtime_dir(rdir: str) -> None:
	if os.path.islink(rdir):
		raise PermissionError(f"Refusing symlink runtime directory: {rdir}")
	os.makedirs(rdir, mode=0o700, exist_ok=True)
	st = os.lstat(rdir)
	if hasattr(os, "getuid") and st.st_uid != os.getuid():
		raise PermissionError(f"Runtime directory {rdir} is not owned by current user")
	try:
		os.chmod(rdir, 0o700)
	except OSError:
		pass


def _daemon_paths(
	cdp_port: Optional[int] = None,
	profile_dir: Optional[str] = None,
	runtime_dir: Optional[str] = None,
	token_path: Optional[str] = None,
) -> dict[str, str]:
	port = CDP_PORT if cdp_port is None else int(cdp_port)
	prof = os.path.expanduser(PROFILE_DIR if profile_dir is None else profile_dir)
	rdir = os.path.expanduser(
		runtime_dir
		if runtime_dir is not None
		else os.environ.get("STEALTH_BROWSER_RUNTIME_DIR", "~/.cache/stealth-browser")
	)
	tpath = os.path.expanduser(
		token_path
		if token_path is not None
		else os.environ.get("STEALTH_BROWSER_TOKEN_FILE", "~/.config/stealth-browser/daemon.token")
	)
	profile_hash = hashlib.sha256(os.path.realpath(prof).encode("utf-8")).hexdigest()[:8]
	stem = f"sb-{port}-{profile_hash}"
	sock_path = os.path.join(rdir, f"{stem}.sock")
	if len(sock_path.encode("utf-8")) > _AF_UNIX_MAX_SAFE_BYTES:
		rdir_hash = hashlib.sha256(os.path.realpath(rdir).encode("utf-8")).hexdigest()[:6]
		uid = os.getuid() if hasattr(os, "getuid") else 0
		rdir = f"/tmp/sb-{uid}"
		stem = f"{stem}-{rdir_hash}"
		sock_path = os.path.join(rdir, f"{stem}.sock")
	return {
		"runtime_dir": rdir,
		"sock_path": sock_path,
		"lock_path": os.path.join(rdir, f"{stem}.lock"),
		"meta_path": os.path.join(rdir, f"{stem}.json"),
		"token_path": tpath,
	}


def _script_sha256() -> str:
	h = hashlib.sha256()
	found = False
	for path in (_server_script_path(), os.path.realpath(__file__)):
		try:
			with open(path, "rb") as f:
				h.update(f.read())
			found = True
		except Exception:
			pass
	return h.hexdigest()[:16] if found else ""


def _parse_loopback_http(addr: str) -> tuple[str, int]:
	raw = (addr or "").strip()
	if not raw:
		raise ValueError("HTTP bind address must not be empty")
	if raw.startswith("["):
		bracket_end = raw.find("]")
		if bracket_end < 0:
			raise ValueError(f"Invalid IPv6 bind address: {addr!r}")
		host = raw[1:bracket_end]
		rest = raw[bracket_end + 1:]
		if not rest.startswith(":"):
			raise ValueError(f"Missing port in HTTP bind address: {addr!r}")
		port_str = rest[1:]
	elif ":" in raw:
		host, port_str = raw.rsplit(":", 1)
	else:
		raise ValueError(f"HTTP bind address must be HOST:PORT (got {addr!r})")
	if host not in ("127.0.0.1", "localhost", "::1"):
		raise ValueError(f"Refusing non-loopback HTTP bind host {host!r}; only 127.0.0.1, localhost, or ::1 are permitted")
	try:
		port = int(port_str)
	except ValueError as exc:
		raise ValueError(f"Invalid port in HTTP bind address {addr!r}") from exc
	if not (1 <= port <= 65535):
		raise ValueError(f"Port out of range (1-65535): {port}")
	return host, port


def _ensure_daemon_token(token_path: Optional[str] = None) -> str:
	path = token_path or _daemon_paths()["token_path"]
	parent = os.path.dirname(path)
	if parent:
		os.makedirs(parent, mode=0o700, exist_ok=True)
		try:
			os.chmod(parent, 0o700)
		except OSError:
			pass
	if os.path.isfile(path):
		try:
			os.chmod(path, 0o600)
		except OSError:
			pass
		with open(path, "r", encoding="utf-8") as f:
			existing = f.read().strip()
		if existing:
			return existing
	token = secrets.token_urlsafe(32)
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	try:
		os.write(fd, (token + "\n").encode("utf-8"))
	finally:
		os.close(fd)
	try:
		os.chmod(path, 0o600)
	except OSError:
		pass
	return token


class _UnixHTTPConnection(http.client.HTTPConnection):
	def __init__(self, sock_path: str, timeout: float = 300.0):
		super().__init__("localhost", timeout=timeout)
		self._sock_path = sock_path

	def connect(self):
		sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		sock.settimeout(self.timeout)
		sock.connect(self._sock_path)
		self.sock = sock


def _uds_request(
	sock_path: str,
	method: str,
	path: str,
	body: Optional[bytes] = None,
	headers: Optional[dict[str, str]] = None,
	timeout: float = 5.0,
) -> tuple[int, dict[str, str], bytes]:
	conn = _UnixHTTPConnection(sock_path, timeout=timeout)
	try:
		req_headers = {"Host": "localhost"}
		if headers:
			req_headers.update(headers)
		conn.request(method, path, body=body, headers=req_headers)
		resp = conn.getresponse()
		data = resp.read()
		resp_headers = {k.lower(): v for k, v in resp.getheaders()}
		return resp.status, resp_headers, data
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _probe_daemon_health(sock_path: str, timeout: float = 1.0) -> Optional[dict]:
	if not os.path.exists(sock_path):
		return None
	try:
		status_code, _, data = _uds_request(sock_path, "GET", "/_daemon/health", timeout=timeout)
		if status_code == 200:
			parsed = json.loads(data.decode("utf-8", errors="ignore"))
			if isinstance(parsed, dict) and parsed.get("ok"):
				return parsed
	except Exception:
		return None
	return None


def _stop_daemon(sock_path: str, meta_path: str, timeout: float = 5.0) -> bool:
	stopped_req = False
	if os.path.exists(sock_path):
		try:
			status_code, _, _ = _uds_request(sock_path, "POST", "/_daemon/stop", body=b"{}", timeout=2.0)
			stopped_req = status_code == 200
		except Exception:
			pass
	if not stopped_req and os.path.isfile(meta_path):
		try:
			with open(meta_path, "r", encoding="utf-8") as f:
				meta = json.load(f)
			pid = int(meta.get("pid", 0))
			if pid > 0:
				os.kill(pid, signal.SIGTERM)
				stopped_req = True
		except Exception:
			pass
	if not stopped_req:
		return False
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if _probe_daemon_health(sock_path, timeout=0.3) is None:
			return True
		time.sleep(0.05)
	return _probe_daemon_health(sock_path, timeout=0.3) is None


def _ensure_daemon_running(
	cdp_port: Optional[int] = None,
	profile_dir: Optional[str] = None,
	runtime_dir: Optional[str] = None,
	idle_exit_s: int = DAEMON_DEFAULT_IDLE_EXIT,
	timeout_s: float = 10.0,
) -> str:
	paths = _daemon_paths(cdp_port=cdp_port, profile_dir=profile_dir, runtime_dir=runtime_dir)
	rdir = paths["runtime_dir"]
	sock_path = paths["sock_path"]
	lock_path = paths["lock_path"]
	meta_path = paths["meta_path"]
	_ensure_secure_runtime_dir(rdir)

	expected_hash = _script_sha256()
	health = _probe_daemon_health(sock_path)
	if health is not None:
		if not expected_hash or health.get("script_hash") == expected_hash:
			return sock_path
		if health.get("in_flight", 0) == 0 and health.get("active_sessions", 0) == 0:
			_stop_daemon(sock_path, meta_path, timeout=3.0)
		else:
			return sock_path

	with open(lock_path, "w", encoding="utf-8") as lock_file:
		fcntl.flock(lock_file, fcntl.LOCK_EX)
		try:
			health = _probe_daemon_health(sock_path)
			if health is not None:
				return sock_path
			if os.path.exists(sock_path):
				try:
					os.unlink(sock_path)
				except OSError:
					pass
			cmd = [
				sys.executable,
				_server_script_path(),
				"--serve",
				"--daemon-idle-exit",
				str(idle_exit_s),
			]
			env = os.environ.copy()
			env.pop("STEALTH_BROWSER_MINIMAL", None)
			env.pop("STEALTH_BROWSER_DISABLE_SECTIONS", None)
			if runtime_dir is not None:
				env["STEALTH_BROWSER_RUNTIME_DIR"] = rdir
			if cdp_port is not None:
				env["STEALTH_BROWSER_CDP_PORT"] = str(cdp_port)
			if profile_dir is not None:
				env["STEALTH_BROWSER_PROFILE_DIR"] = profile_dir
			proc = subprocess.Popen(
				cmd,
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				start_new_session=True,
				env=env,
			)
			deadline = time.monotonic() + timeout_s
			while time.monotonic() < deadline:
				if proc.poll() is not None:
					raise RuntimeError(f"Shared daemon exited early with code {proc.returncode}")
				if _probe_daemon_health(sock_path, timeout=0.3) is not None:
					return sock_path
				time.sleep(0.05)
			raise TimeoutError(f"Timed out waiting for stealth-browser daemon at {sock_path}")
		finally:
			fcntl.flock(lock_file, fcntl.LOCK_UN)


def _extract_mcp_payloads(content_type: str, data: bytes) -> list[bytes]:
	if not data or not data.strip():
		return []
	if "text/event-stream" in (content_type or "").lower():
		payloads: list[bytes] = []
		data_lines: list[str] = []
		for raw_line in data.decode("utf-8", errors="replace").splitlines():
			if raw_line.startswith("data:"):
				data_lines.append(raw_line[5:].lstrip(" "))
			elif raw_line == "" and data_lines:
				joined = "\n".join(data_lines).strip()
				if joined:
					payloads.append(joined.encode("utf-8"))
				data_lines.clear()
		if data_lines:
			joined = "\n".join(data_lines).strip()
			if joined:
				payloads.append(joined.encode("utf-8"))
		return payloads
	return [data.strip()]


class _StdioUdsProxy:
	def __init__(
		self,
		sock_path: str,
		in_stream=None,
		out_stream=None,
		request_timeout: float = 300.0,
		reconnect_fn=None,
		disabled_tools: frozenset[str] = frozenset(),
		gating_notice: str = "",
	):
		self.sock_path = sock_path
		self.in_stream = in_stream if in_stream is not None else sys.stdin.buffer
		self.out_stream = out_stream if out_stream is not None else sys.stdout.buffer
		self.request_timeout = request_timeout
		self.reconnect_fn = reconnect_fn
		self.disabled_tools = frozenset(disabled_tools) - PROTECTED_TOOLS
		self.gating_notice = gating_notice
		self.session_id: Optional[str] = None
		self._write_lock = threading.Lock()
		self._state_lock = threading.Lock()
		self._init_raw: Optional[bytes] = None
		self._initialized_raw: Optional[bytes] = None

	def _filter_response_payload(self, payload: bytes, method: str) -> bytes:
		if not self.disabled_tools or method not in ("tools/list", "initialize"):
			return payload
		try:
			obj = json.loads(payload.decode("utf-8"))
		except Exception:
			return payload
		if not isinstance(obj, dict):
			return payload
		result = obj.get("result")
		if not isinstance(result, dict):
			return payload
		changed = False
		if method == "tools/list" and isinstance(result.get("tools"), list):
			result["tools"] = [
				t for t in result["tools"]
				if not (isinstance(t, dict) and t.get("name") in self.disabled_tools)
			]
			changed = True
		elif method == "initialize" and self.gating_notice:
			existing = result.get("instructions")
			if isinstance(existing, str) and existing:
				if self.gating_notice not in existing:
					result["instructions"] = f"{existing}\n{self.gating_notice}"
					changed = True
			else:
				result["instructions"] = self.gating_notice
				changed = True
		if not changed:
			return payload
		return json.dumps(obj).encode("utf-8")

	def _write_line(self, payload: bytes) -> None:
		line = payload.rstrip(b"\r\n") + b"\n"
		with self._write_lock:
			self.out_stream.write(line)
			self.out_stream.flush()

	def _rewrite_error_id(self, payload: bytes, msg_id: Any) -> bytes:
		if msg_id is None or b'"error"' not in payload:
			return payload
		try:
			obj = json.loads(payload.decode("utf-8"))
		except Exception:
			return payload
		if isinstance(obj, dict) and "error" in obj and obj.get("id") != msg_id:
			obj["id"] = msg_id
			return json.dumps(obj).encode("utf-8")
		return payload

	def _timeout_for_request(self, method: str, parsed: Any) -> float:
		if method == "tools/call" and isinstance(parsed, dict):
			params = parsed.get("params")
			if isinstance(params, dict) and params.get("name") == "wait_for_human":
				args = params.get("arguments")
				raw_t = args.get("timeout_s", 180) if isinstance(args, dict) else 180
				try:
					wait_t = float(raw_t)
				except (TypeError, ValueError):
					wait_t = 180.0
				return max(self.request_timeout, wait_t + 30.0)
		return self.request_timeout

	def _post_mcp(
		self,
		body: bytes,
		session_id: Optional[str],
		timeout: Optional[float] = None,
	) -> tuple[int, dict[str, str], bytes]:
		headers = {
			"Content-Type": "application/json",
			"Accept": "application/json, text/event-stream",
		}
		if session_id:
			headers["Mcp-Session-Id"] = session_id
		req_timeout = self.request_timeout if timeout is None else timeout
		return _uds_request(self.sock_path, "POST", "/mcp", body=body, headers=headers, timeout=req_timeout)

	def _try_reconnect(self) -> bool:
		if self.reconnect_fn is None or self._init_raw is None:
			return False
		with self._state_lock:
			try:
				self.sock_path = self.reconnect_fn()
				status_code, resp_headers, _ = self._post_mcp(self._init_raw, None)
				if status_code != 200:
					return False
				new_sid = resp_headers.get("mcp-session-id")
				if new_sid:
					self.session_id = new_sid
				if self._initialized_raw is not None:
					self._post_mcp(self._initialized_raw, self.session_id)
				return True
			except Exception:
				return False

	def _forward_message(self, raw: bytes, method: str, msg_id: Any, parsed: Any = None) -> None:
		if self.disabled_tools and method == "tools/call" and isinstance(parsed, dict):
			params = parsed.get("params")
			tool_name = params.get("name", "") if isinstance(params, dict) else ""
			if tool_name in self.disabled_tools:
				if msg_id is not None:
					err = json.dumps({
						"jsonrpc": "2.0",
						"id": msg_id,
						"error": {
							"code": -32602,
							"message": f"Tool '{tool_name}' is disabled in this session",
						},
					}).encode("utf-8")
					self._write_line(err)
				return

		if method != "initialize" and self.session_id is None:
			if msg_id is not None:
				err = json.dumps({
					"jsonrpc": "2.0",
					"id": msg_id,
					"error": {"code": -32601, "message": f"Method not found: {method}"},
				}).encode("utf-8")
				self._write_line(err)
			return

		req_timeout = self._timeout_for_request(method, parsed)
		try:
			status_code, resp_headers, data = self._post_mcp(raw, self.session_id, timeout=req_timeout)
			if status_code in (404, 502, 503) and method != "initialize" and self._try_reconnect():
				status_code, resp_headers, data = self._post_mcp(raw, self.session_id, timeout=req_timeout)
		except (OSError, http.client.HTTPException):
			if method != "initialize" and self._try_reconnect():
				try:
					status_code, resp_headers, data = self._post_mcp(raw, self.session_id, timeout=req_timeout)
				except Exception as exc:
					if msg_id is not None:
						err = json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": f"Daemon connection failed: {exc}"}}).encode("utf-8")
						self._write_line(err)
					return
			else:
				if msg_id is not None:
					err = json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": "Daemon connection failed"}}).encode("utf-8")
					self._write_line(err)
				return
		except Exception as exc:
			if msg_id is not None:
				err = json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}}).encode("utf-8")
				self._write_line(err)
			return

		new_sid = resp_headers.get("mcp-session-id")
		if new_sid and 200 <= status_code < 300:
			with self._state_lock:
				self.session_id = new_sid

		if status_code == 202 or msg_id is None:
			return
		payloads = _extract_mcp_payloads(resp_headers.get("content-type", ""), data)
		if payloads:
			for payload in payloads:
				payload = self._rewrite_error_id(payload, msg_id)
				self._write_line(self._filter_response_payload(payload, method))
		elif status_code >= 400:
			msg = data.decode("utf-8", errors="replace").strip() or f"HTTP {status_code}"
			err = json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": msg}}).encode("utf-8")
			self._write_line(err)

	def close_session(self) -> None:
		sid = self.session_id
		if not sid:
			return
		try:
			_uds_request(self.sock_path, "DELETE", "/mcp", headers={"Mcp-Session-Id": sid}, timeout=2.0)
		except Exception:
			pass

	def run(self) -> int:
		threads: list[threading.Thread] = []
		try:
			while True:
				line = self.in_stream.readline()
				if not line:
					break
				raw = line.strip()
				if not raw:
					continue
				try:
					parsed = json.loads(raw.decode("utf-8"))
				except Exception:
					continue
				method = parsed.get("method", "") if isinstance(parsed, dict) else ""
				msg_id = parsed.get("id") if isinstance(parsed, dict) else None
				if method == "initialize":
					self._init_raw = raw
					self._forward_message(raw, method, msg_id, parsed)
				elif method == "notifications/initialized":
					self._initialized_raw = raw
					self._forward_message(raw, method, msg_id, parsed)
				else:
					t = threading.Thread(target=self._forward_message, args=(raw, method, msg_id, parsed), daemon=True)
					t.start()
					threads.append(t)
					threads = [th for th in threads if th.is_alive()]
			for t in threads:
				t.join(timeout=5.0)
			return 0
		finally:
			self.close_session()


def _run_daemon_status(runtime_dir: Optional[str] = None) -> int:
	paths = _daemon_paths(runtime_dir=runtime_dir)
	health = _probe_daemon_health(paths["sock_path"])
	if health is None:
		print(f"Daemon: stopped (socket: {paths['sock_path']})")
		return 1
	mode = oct(os.stat(paths["sock_path"]).st_mode & 0o777) if os.path.exists(paths["sock_path"]) else "?"
	print(
		f"Daemon: running (pid={health.get('pid')}, version={health.get('version')}, "
		f"sessions={health.get('active_sessions', 0)}, in_flight={health.get('in_flight', 0)}, "
		f"socket={paths['sock_path']} mode={mode})"
	)
	return 0


def _run_daemon_stop(runtime_dir: Optional[str] = None) -> int:
	paths = _daemon_paths(runtime_dir=runtime_dir)
	if _probe_daemon_health(paths["sock_path"]) is None and not os.path.isfile(paths["meta_path"]):
		print(f"Daemon: already stopped (socket: {paths['sock_path']})")
		return 0
	ok = _stop_daemon(paths["sock_path"], paths["meta_path"])
	if ok:
		print(f"Daemon: stopped (socket: {paths['sock_path']})")
		return 0
	print(f"Daemon: failed to stop cleanly (socket: {paths['sock_path']})", file=sys.stderr)
	return 1


def _browser_pids_for_profile(profile_dir: str) -> list[int]:
	markers = [f"--user-data-dir={profile_dir}", f"--user-data-dir={os.path.realpath(profile_dir)}"]
	try:
		proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10)
	except Exception:
		return []
	pids: list[int] = []
	for line in proc.stdout.splitlines():
		line = line.strip()
		if not any(marker in line for marker in markers):
			continue
		if "Chromium" not in line and "chrome" not in line.lower():
			continue
		fields = line.split(None, 1)
		if not fields:
			continue
		try:
			pid = int(fields[0])
		except ValueError:
			continue
		if pid not in (os.getpid(), os.getppid()) and pid not in pids:
			pids.append(pid)
	return pids


def _pid_alive(pid: int) -> bool:
	try:
		os.kill(pid, 0)
		return True
	except ProcessLookupError:
		return False
	except PermissionError:
		return True


def _run_kill_all(runtime_dir: Optional[str] = None) -> int:
	paths = _daemon_paths(runtime_dir=runtime_dir)
	profile_dir = os.path.expanduser(PROFILE_DIR)
	_run_daemon_stop(runtime_dir=runtime_dir)
	pids = _browser_pids_for_profile(profile_dir)
	for pid in pids:
		try:
			os.kill(pid, signal.SIGTERM)
		except OSError:
			pass
	remaining = [pid for pid in pids if _pid_alive(pid)]
	deadline = time.monotonic() + 3.0
	while remaining and time.monotonic() < deadline:
		time.sleep(0.1)
		remaining = [pid for pid in remaining if _pid_alive(pid)]
	for pid in remaining:
		try:
			os.kill(pid, signal.SIGKILL)
		except OSError:
			pass
	removed = 0
	paths_to_clear = [os.path.join(profile_dir, name) for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")]
	paths_to_clear.extend([
		os.path.join(paths["runtime_dir"], f"state-{CDP_PORT}.json"),
		os.path.join(paths["runtime_dir"], f"launch-{CDP_PORT}.lock"),
	])
	for path in paths_to_clear:
		if os.path.lexists(path):
			try:
				os.unlink(path)
				removed += 1
			except OSError:
				pass
	print(f"Killed {len(pids)} browser process(es); cleared {removed} lock/state file(s)")
	return 0


def _run_stdio_proxy(
	runtime_dir: Optional[str] = None,
	idle_exit_s: int = DAEMON_DEFAULT_IDLE_EXIT,
	disabled_tools: frozenset[str] = frozenset(),
	gating_notice: str = "",
) -> int:
	def _ensure():
		return _ensure_daemon_running(runtime_dir=runtime_dir, idle_exit_s=idle_exit_s)

	sock_path = _ensure()
	proxy = _StdioUdsProxy(
		sock_path,
		reconnect_fn=_ensure,
		disabled_tools=disabled_tools,
		gating_notice=gating_notice,
	)
	return proxy.run()


def _is_cli_fast_path(argv: Optional[list[str]] = None) -> bool:
	args = sys.argv[1:] if argv is None else argv
	return not any(arg in _HEAVY_CLI_FLAGS or arg.startswith("--http") for arg in args)


def _run_cli_fast_path() -> int:
	args = _build_arg_parser().parse_args()
	if args.list_sections:
		print(_format_sections_list())
		return 0
	if args.daemon_status:
		return _run_daemon_status()
	if args.daemon_stop:
		return _run_daemon_stop()
	if args.kill_all:
		return _run_kill_all()
	try:
		disabled_tools, disabled_sections, effective_minimal = _resolve_disabled_tools(
			minimal=args.minimal,
			disabled_sections=_disabled_sections_from_args(args),
		)
	except ValueError as exc:
		print(f"Error: {exc}", file=sys.stderr)
		return 2
	notice = _gating_notice(effective_minimal, disabled_sections, disabled_tools)

	def _handle_signal(_signum, _frame):
		raise SystemExit(0)

	for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
		signal.signal(sig, _handle_signal)
	try:
		return _run_stdio_proxy(
			idle_exit_s=args.daemon_idle_exit,
			disabled_tools=disabled_tools,
			gating_notice=notice,
		)
	except (KeyboardInterrupt, SystemExit):
		return 0


def main() -> None:
	if _is_cli_fast_path():
		raise SystemExit(_run_cli_fast_path())
	script_dir = os.path.dirname(os.path.realpath(__file__))
	if script_dir not in sys.path:
		sys.path.insert(0, script_dir)
	import stealth_browser

	stealth_browser.main()


if __name__ == "__main__":
	main()

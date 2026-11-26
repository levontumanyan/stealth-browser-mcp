#!/usr/bin/env python3
"""Lightweight offline (+ optional external) stealth regression probe for stealth-browser."""

import argparse
import asyncio
import datetime
import http.server
import importlib.machinery
import importlib.util
import json
import os
import platform
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "stealth-browser"
DEFAULT_EXTERNAL_URL = "https://nowsecure.nl/"

PROBE_HTML = """<!doctype html>
<html>
<head><title>Stealth Regression Probe</title></head>
<body>
<img id="probe-img" src="/pixel.png" alt="probe">
<script>
window.__runStealthProbe = () => {
	let runtimeLeak = false;
	const err = new Error();
	Object.defineProperty(err, 'stack', {
		get() { runtimeLeak = true; return ''; }
	});
	console.debug(err);

	let webglVendor = '';
	let webglRenderer = '';
	try {
		const canvas = document.createElement('canvas');
		const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
		if (gl) {
			const dbg = gl.getExtension('WEBGL_debug_renderer_info');
			if (dbg) {
				webglVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || '';
				webglRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '';
			}
		}
	} catch (_) {}

	return {
		webdriver: navigator.webdriver,
		userAgent: navigator.userAgent,
		hasChrome: typeof window.chrome === 'object' && window.chrome !== null,
		pluginsLength: navigator.plugins ? navigator.plugins.length : -1,
		languages: Array.from(navigator.languages || []),
		platform: navigator.platform || '',
		webglVendor,
		webglRenderer,
		screen: {
			width: screen.width,
			height: screen.height,
			availWidth: screen.availWidth,
			availHeight: screen.availHeight,
			dpr: window.devicePixelRatio,
		},
		runtimeLeak,
		cdcProps: Object.keys(window).filter(k => /cdc_|__webdriver|__playwright|__pw_/.test(k)),
	};
};
</script>
</body>
</html>"""

PNG_1X1 = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
	b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
	b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
	def log_message(self, format: str, *args: Any) -> None:
		pass

	def do_GET(self) -> None:
		if self.path == "/pixel.png":
			self.send_response(200)
			self.send_header("Content-Type", "image/png")
			self.send_header("Content-Length", str(len(PNG_1X1)))
			self.end_headers()
			self.wfile.write(PNG_1X1)
			return
		body = PROBE_HTML.encode("utf-8")
		self.send_response(200)
		self.send_header("Content-Type", "text/html; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)


def _pick_free_port() -> int:
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.bind(("127.0.0.1", 0))
		return int(sock.getsockname()[1])


def _inspect_chromium_cmdline(cdp_port: int) -> dict[str, Any]:
	try:
		res = subprocess.run(["ps", "-axo", "args"], capture_output=True, text=True, timeout=5)
		lines = [ln for ln in res.stdout.splitlines() if f"--remote-debugging-port={cdp_port}" in ln]
	except Exception as exc:
		return {"found": False, "error": str(exc), "enable_automation": False, "disable_blink_features": False}
	if not lines:
		return {"found": False, "enable_automation": False, "disable_blink_features": False}
	cmd = lines[0]
	return {
		"found": True,
		"enable_automation": "--enable-automation" in cmd,
		"disable_blink_features": "--disable-blink-features=AutomationControlled" in cmd,
	}


def _query_browser_build(cdp_port: int) -> str:
	try:
		with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2.0) as resp:
			data = json.loads(resp.read().decode("utf-8", errors="ignore"))
			return str(data.get("Browser") or "")
	except Exception:
		return ""


def grade_local_probe(signals: dict[str, Any], cmdline: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
	checks: dict[str, dict[str, Any]] = {}

	webdriver = bool(signals.get("webdriver"))
	checks["navigator_webdriver"] = {
		"status": "FAIL" if webdriver else "PASS",
		"value": webdriver,
	}

	runtime_leak = bool(signals.get("runtimeLeak"))
	checks["runtime_enable_leak"] = {
		"status": "FAIL" if runtime_leak else "PASS",
		"value": runtime_leak,
	}

	cdc_props = list(signals.get("cdcProps") or [])
	checks["cdc_globals"] = {
		"status": "FAIL" if cdc_props else "PASS",
		"value": cdc_props,
	}

	has_chrome = bool(signals.get("hasChrome"))
	checks["window_chrome"] = {
		"status": "PASS" if has_chrome else "FAIL",
		"value": has_chrome,
	}

	plugins_len = int(signals.get("pluginsLength", -1))
	checks["navigator_plugins"] = {
		"status": "PASS" if plugins_len > 0 else "DEGRADED",
		"value": plugins_len,
	}

	ua = str(signals.get("userAgent") or "")
	checks["user_agent"] = {
		"status": "DEGRADED" if ("HeadlessChrome" in ua or not ua) else "PASS",
		"value": ua,
	}

	renderer = str(signals.get("webglRenderer") or "")
	vendor = str(signals.get("webglVendor") or "")
	webgl_degraded = (not renderer) or ("swiftshader" in renderer.lower())
	checks["webgl_renderer"] = {
		"status": "DEGRADED" if webgl_degraded else "PASS",
		"value": {"vendor": vendor, "renderer": renderer},
	}

	screen = signals.get("screen") or {}
	w = int(screen.get("width", 0) or 0)
	h = int(screen.get("height", 0) or 0)
	checks["screen_dimensions"] = {
		"status": "DEGRADED" if (w <= 800 and h <= 600) else "PASS",
		"value": screen,
	}

	if not cmdline.get("found"):
		checks["chromium_cmdline_flags"] = {
			"status": "DEGRADED",
			"value": cmdline,
		}
	elif cmdline.get("enable_automation"):
		checks["chromium_cmdline_flags"] = {
			"status": "FAIL",
			"value": cmdline,
		}
	elif not cmdline.get("disable_blink_features"):
		checks["chromium_cmdline_flags"] = {
			"status": "DEGRADED",
			"value": cmdline,
		}
	else:
		checks["chromium_cmdline_flags"] = {
			"status": "PASS",
			"value": cmdline,
		}

	statuses = {c["status"] for c in checks.values()}
	if "FAIL" in statuses:
		overall = "FAIL"
	elif "DEGRADED" in statuses:
		overall = "DEGRADED"
	else:
		overall = "PASS"
	return overall, checks


def grade_external_result(
	*,
	ok: bool,
	title: str = "",
	body_snippet: str = "",
	error: str = "",
) -> dict[str, Any]:
	if not ok:
		low = error.lower()
		net_markers = ("err_", "timeout", "timed out", "unreachable", "name_not_resolved", "connection", "network")
		status = "UNREACHABLE" if any(m in low for m in net_markers) else "UNREACHABLE"
		return {"status": status, "error": error}
	combined = f"{title} {body_snippet}".lower()
	if any(k in combined for k in ("just a moment", "checking your browser", "attention required", "access denied")):
		return {"status": "FAIL", "title": title}
	if "oh yeah" in combined or "nowsecure" in combined:
		return {"status": "PASS", "title": title}
	return {"status": "DEGRADED", "title": title}


def _load_server_module(cdp_port: int, profile_dir: str, headless: bool):
	os.environ["STEALTH_BROWSER_CDP_PORT"] = str(cdp_port)
	os.environ["STEALTH_BROWSER_PROFILE_DIR"] = profile_dir
	os.environ["STEALTH_BROWSER_HEADLESS"] = "true" if headless else "false"
	os.environ["STEALTH_BROWSER_IDLE_TIMEOUT"] = "60"
	loader = importlib.machinery.SourceFileLoader("stealth_browser_probe_mod", str(SCRIPT_PATH))
	spec = importlib.util.spec_from_loader(loader.name, loader)
	mod = importlib.util.module_from_spec(spec)
	sys.modules[loader.name] = mod
	loader.exec_module(mod)
	return mod


async def run_probe(
	*,
	external: bool = False,
	external_url: str = DEFAULT_EXTERNAL_URL,
	external_timeout_s: int = 20,
	headless: bool = True,
	cdp_port: int = 0,
) -> dict[str, Any]:
	port = cdp_port or _pick_free_port()
	profile_dir = tempfile.mkdtemp(prefix="sb-stealth-probe-")
	srv = socketserver.TCPServer(("127.0.0.1", 0), _ProbeHandler)
	http_port = srv.server_address[1]
	thread = threading.Thread(target=srv.serve_forever, daemon=True)
	thread.start()

	sb = _load_server_module(port, profile_dir, headless)
	try:
		local_url = f"http://127.0.0.1:{http_port}/"
		await sb.navigate(local_url, block_media=False)
		page = await sb._resolve_page(-1)
		signals = await page.evaluate("window.__runStealthProbe()", isolated_context=False)
		cmdline = _inspect_chromium_cmdline(port)
		browser_build = _query_browser_build(port)
		local_status, local_checks = grade_local_probe(signals, cmdline)

		targets: dict[str, Any] = {
			"local": {
				"status": local_status,
				"network_blocker_attached": page in sb.network_blocker._sessions,
				"checks": local_checks,
			}
		}

		if external:
			try:
				nav_out = await asyncio.wait_for(
					sb.navigate(external_url, block_media=False, timeout_ms=external_timeout_s * 1000),
					timeout=external_timeout_s + 5,
				)
				if nav_out.startswith("Navigation error:"):
					targets["external"] = {"url": external_url, **grade_external_result(ok=False, error=nav_out)}
				else:
					await asyncio.sleep(3.0)
					title = await page.title()
					snippet = str(await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''"))
					targets["external"] = {
						"url": external_url,
						**grade_external_result(ok=True, title=title, body_snippet=snippet),
					}
			except Exception as exc:
				targets["external"] = {"url": external_url, **grade_external_result(ok=False, error=str(exc))}

		return {
			"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
			"tool_version": getattr(sb, "__version__", "unknown"),
			"python_version": platform.python_version(),
			"os": f"{platform.system()} {platform.release()} ({platform.machine()})",
			"browser_build": browser_build,
			"mode": "headless" if headless else "headed",
			"overall_status": local_status,
			"targets": targets,
		}
	finally:
		try:
			await sb.close_browser(force_kill=True)
		except Exception:
			pass
		srv.shutdown()
		srv.server_close()
		shutil.rmtree(profile_dir, ignore_errors=True)


def _format_human(report: dict[str, Any]) -> str:
	lines = [
		f"Stealth Probe [{report['mode']}] — Overall: {report['overall_status']}",
		f"Browser: {report['browser_build'] or 'unknown'} | Python: {report['python_version']} | OS: {report['os']}",
	]
	local = report["targets"]["local"]
	for name, info in local["checks"].items():
		lines.append(f"  [{info['status']:8s}] {name}: {info['value']}")
	if "external" in report["targets"]:
		ext = report["targets"]["external"]
		lines.append(f"  [{ext['status']:8s}] external ({ext['url']}): {ext.get('title') or ext.get('error', '')}")
	return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Run offline (+ optional external) stealth regression checks against stealth-browser.",
	)
	parser.add_argument(
		"--json",
		nargs="?",
		const="-",
		default=None,
		metavar="PATH",
		help="Emit JSON report to stdout (if no PATH or '-') or write to PATH.",
	)
	parser.add_argument(
		"--external",
		action="store_true",
		help="Also probe an external Cloudflare-style target (unreachable maps to UNREACHABLE, never FAIL).",
	)
	parser.add_argument(
		"--external-url",
		default=DEFAULT_EXTERNAL_URL,
		help=f"External target URL when --external is set (default: {DEFAULT_EXTERNAL_URL}).",
	)
	parser.add_argument(
		"--external-timeout",
		type=int,
		default=20,
		help="Timeout in seconds for the external target (default: 20).",
	)
	parser.add_argument(
		"--headed",
		action="store_true",
		help="Launch Chromium in headed mode instead of headless.",
	)
	parser.add_argument(
		"--port",
		type=int,
		default=0,
		help="CDP port to use (default: auto-selected free localhost port).",
	)
	return parser


def main(argv: Optional[list[str]] = None) -> int:
	args = build_parser().parse_args(argv)
	report = asyncio.run(
		run_probe(
			external=args.external,
			external_url=args.external_url,
			external_timeout_s=args.external_timeout,
			headless=not args.headed,
			cdp_port=args.port,
		)
	)
	if args.json is not None:
		payload = json.dumps(report, indent=2)
		if args.json == "-":
			print(payload)
		else:
			out_path = Path(args.json)
			out_path.parent.mkdir(parents=True, exist_ok=True)
			out_path.write_text(payload + "\n", encoding="utf-8")
			print(f"Wrote stealth probe JSON to {out_path}")
	else:
		print(_format_human(report))
	return 1 if report["overall_status"] == "FAIL" else 0


if __name__ == "__main__":
	raise SystemExit(main())

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
# 	"mcp==1.30.0",
# 	"patchright==1.64.4",
# ]
# ///
"""FastMCP server providing browser automation tools over CDP / stealth Chromium using patchright."""

__version__ = "0.1.0"

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if _SCRIPT_DIR not in sys.path:
	sys.path.insert(0, _SCRIPT_DIR)

import argparse
import fcntl
import hashlib
import http.client
import json
import secrets
import signal
import socket
import subprocess
import threading
import time
from typing import Any, Optional

from stealth_browser_cli import (
	CDP_PORT,
	CDP_URL,
	DAEMON_DEFAULT_HTTP,
	DAEMON_DEFAULT_IDLE_EXIT,
	PROFILE_DIR,
	PROTECTED_TOOLS,
	TOOL_SECTIONS,
	_HEAVY_CLI_FLAGS,
	_StdioUdsProxy,
	_UnixHTTPConnection,
	_build_arg_parser,
	_daemon_paths,
	_disabled_sections_from_args,
	_ensure_daemon_running,
	_ensure_daemon_token,
	_ensure_secure_runtime_dir,
	_extract_mcp_payloads,
	_format_sections_list,
	_gating_notice,
	_is_cli_fast_path as _cli_is_fast_path,
	_parse_loopback_http,
	_probe_daemon_health,
	_resolve_disabled_tools,
	_run_cli_fast_path,
	_run_daemon_status,
	_run_daemon_stop,
	_run_stdio_proxy,
	_script_sha256,
	_server_script_path,
	_stop_daemon,
	_uds_request,
)


def _is_cli_fast_path() -> bool:
	argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
	if __name__ != "__main__" and argv0 not in ("stealth-browser", "stealth_browser.py"):
		return False
	return _cli_is_fast_path()


# Fast-path exits before importing mcp/patchright so legacy script invocation stays lightweight.
if _is_cli_fast_path():
	raise SystemExit(_run_cli_fast_path())

import asyncio
import base64
import collections
import contextlib
import contextvars
import datetime
import functools
import hmac
import io
import logging
import random
import re
import shutil
import struct
import tempfile
import urllib.request
import weakref
import zipfile
from collections import deque
from typing import Union
from urllib.parse import urlparse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from patchright.async_api import Browser, BrowserContext, Page, async_playwright

try:
	from pydantic import ConfigDict
	import mcp.server.fastmcp.utilities.func_metadata as _fm

	class _StrictArgModel(_fm.ArgModelBase):
		model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

	_fm.ArgModelBase = _StrictArgModel
except Exception:
	pass

@contextlib.asynccontextmanager
async def _lifespan(_app):
	loop = asyncio.get_running_loop()

	def _on_task_exc(_loop, context):
		try:
			session._unhandled_task_exceptions["n"] += 1
		except Exception:
			pass
		logging.error("UNHANDLED task exception: %r", context.get("exception"))

	loop.set_exception_handler(_on_task_exc)
	try:
		session._unhandled_task_exceptions = {"n": 0}
	except Exception:
		pass
	yield

STEALTH_BROWSER_CHEATSHEET = (
	"The browser runs headed in the background by default and never steals focus. Use wait_for_human() for interactive login/2FA/CAPTCHA — it only sends a macOS notification naming the tab; never call set_headless() unless the user asks.\n"
	"Use this cheat-sheet instead of reading individual tool schemas; prefer snapshot(filter=...)/extract()/batch() over evaluate_js(). tab=-1 targets active tab (or index / stable 'tN').\n"
	"Always call wait_for_human() (not set_headless churn) whenever a flow hits login/2FA/CAPTCHA/MFA.\n"
	"- status() | new_tab(url='about:blank', block_media=None) | switch_tab(tab=0) | close_tab(tab=-1)\n"
	"  For tab URLs/titles: call status() — it lists every open tab's title and URL as [index | id | name] in one call; do NOT evaluate_js location.href/document.title.\n"
	"  Tab ids derive from the browser tab itself, so they are unique across concurrent agents; pass tab=<id> or tab=<name substring> on every call.\n"
	"- navigate(url, block_media=None, new_tab=False, tab=-1, timeout_ms=30000, wait_until='domcontentloaded')\n"
	"- wait_for(selector='', text='', url_contains='', state='visible', timeout_ms=15000, tab=-1)\n"
	"- wait_for_human(reason='...', url_contains='', url_not_contains='', selector='', timeout_s=180, poll_interval_s=1.5, notify=True, tab=-1)\n"
	"- snapshot(selector='body', max_elements=80, tab=-1, include_shadow=True, include_iframes=True, include_hrefs=False, filter='', viewport_only=False, max_length=8000) — pass include_hrefs=True for link URLs\n"
	"- get_content(selector='body', max_length=4000, interactive_only=False, clean=True, tab=-1, offset=0, filter='')\n"
	"- extract(container_selector, fields={k:'text:.sel'|'@attr .sel'|'html:.sel'}, limit=20, include_shadow=True, include_iframes=True, tab=-1, max_length=8000)\n"
	"  For form/input values: extract('#form', fields={'price': '@value #priceInput', 'agree': '@checked #agree'}) — do NOT use evaluate_js.\n"
	"- click(selector='', ref='', text='', force=False, tab=-1, timeout_ms=5000)\n"
	"- type(selector='', text='', clear_first=True, press_enter=False, tab=-1, timeout_ms=5000, ref='')\n"
	"- press_key(key, tab=-1) | scroll(direction='down', amount=600, tab=-1) | screenshot(save_path='', tab=-1, full_page=False, selector='', ref='', annotate=False)\n"
	"- batch(actions=[{action:'click'|'fill'|'type'|'select'|'press'|'wait_for'|'sleep'|'evaluate', selector, ref, text, value, key, state, ms, force, expression, main_world, timeout_ms}], tab=-1)\n"
	"- evaluate_js(expression='', tab=-1, timeout_ms=15000, script='', max_length=8000, main_world=False)\n"
	"- fetch(url, method='GET', headers=None, body=None, response_type='auto', tab=-1, timeout_ms=15000, save_path='', max_length=8000)\n"
	"- state(framework='auto', selector='', tab=-1, max_length=8000) | set_media(enable_media=True, tab=-1) | set_animations(enabled=False, persist=True)\n"
	"- downloads(limit=20, include_dir=False) | network_requests(url_filter='', resource_type='', content_type='', status_min=0, limit=30, clear=False, tab=-1, max_length=8000) | console_logs(level='', limit=50, clear=False, tab=-1, max_length=8000)\n"
	"- keep_alive(enable=True, tab=-1, min_seconds=45, max_seconds=90, ping_url=None, click_selector=None) | set_headless(headless=True, force=False) | restart_browser(headless=None, url=None, force=False) | close_browser(force_kill=True, force=False)"
)

mcp = FastMCP(
	"stealth-browser",
	instructions=STEALTH_BROWSER_CHEATSHEET,
	lifespan=_lifespan,
	transport_security=TransportSecuritySettings(
		enable_dns_rebinding_protection=True,
		allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "localhost", "127.0.0.1"],
	),
)
mcp._mcp_server.version = __version__


@mcp.resource("instructions://cheatsheet")
def stealth_browser_cheatsheet() -> str:
	"""Curated cheat-sheet for stealth-browser tools (use instead of reading raw schema JSON)."""
	return STEALTH_BROWSER_CHEATSHEET


_CHEATSHEET_FILE = os.path.expanduser("~/.cache/stealth-browser/cheatsheet.txt")
try:
	os.makedirs(os.path.dirname(_CHEATSHEET_FILE), exist_ok=True)
	with open(_CHEATSHEET_FILE, "w", encoding="utf-8") as _f:
		_f.write(STEALTH_BROWSER_CHEATSHEET)
except Exception:
	pass

DOWNLOADS_DIR = os.path.expanduser(os.environ.get("STEALTH_BROWSER_DOWNLOADS_DIR", "~/Downloads"))
IDLE_TIMEOUT_SECONDS = int(os.environ.get("STEALTH_BROWSER_IDLE_TIMEOUT", "300"))
CDP_CONNECT_TIMEOUT_S = 5.0
CDP_OP_TIMEOUT_S = 5.0
GET_CONTEXT_TIMEOUT_S = 30.0
PAGE_TITLE_TIMEOUT_S = 5.0
PAGE_CLOSE_TIMEOUT_S = 5.0
BROWSER_CLOSE_TIMEOUT_S = 5.0
PLAYWRIGHT_START_TIMEOUT_S = 15.0
PLAYWRIGHT_STOP_TIMEOUT_S = 5.0
NEW_PAGE_TIMEOUT_S = 10.0
RESTART_BROWSER_TIMEOUT_S = 30.0
GOTO_OUTER_BUFFER_S = 5.0
DIALOG_ACTION_TIMEOUT_S = 3.0
MAX_DIALOG_HISTORY = 50

TOOL_TOTAL_TIMEOUT_S = 60.0
CDP_PROBE_STALE_S = 15.0
CDP_PROBE_RETRY_SLEEP_S = 0.25
_WRITE_PATH_TOOLS = frozenset({
	"navigate",
	"new_tab",
	"click",
	"type",
	"press_key",
	"scroll",
	"batch",
	"close_tab",
	"evaluate_js",
	"fetch",
})
_CACHED_READ_TOOLS = frozenset({"status", "downloads", "console_logs", "network_requests"})


def _with_tool_deadline(fn):
	@functools.wraps(fn)
	async def wrapper(*args, **kwargs):
		tool_name = fn.__name__
		if tool_name in _WRITE_PATH_TOOLS and hasattr(session, "ensure_cdp_healthy"):
			try:
				await session.ensure_cdp_healthy()
			except Exception:
				pass
		try:
			result = await asyncio.wait_for(fn(*args, **kwargs), timeout=TOOL_TOTAL_TIMEOUT_S)
			if tool_name not in _CACHED_READ_TOOLS and getattr(session, "_context", None) is not None:
				if isinstance(result, str) and result.startswith("Error"):
					if hasattr(session, "mark_cdp_error"):
						session.mark_cdp_error()
				elif hasattr(session, "mark_cdp_ok"):
					session.mark_cdp_ok()
			return result
		except asyncio.TimeoutError:
			if hasattr(session, "mark_cdp_error"):
				session.mark_cdp_error()
			try:
				probe_ok = await asyncio.wait_for(asyncio.to_thread(session._probe_cdp_ready), timeout=2.0)
			except Exception:
				probe_ok = False
			if not probe_ok:
				if hasattr(session, "_recover_wedged_cdp"):
					try:
						await session._recover_wedged_cdp()
					except Exception:
						pass
				else:
					try:
						await asyncio.wait_for(session.close(), timeout=5.0)
					except Exception:
						pass
					try:
						await asyncio.wait_for(asyncio.to_thread(_force_kill_browser_processes), timeout=10.0)
					except Exception:
						pass
				return (
					f"Error {tool_name}: exceeded {int(TOOL_TOTAL_TIMEOUT_S)}s total; "
					f"CDP transport wedged — browser force-restarted, retry the call."
				)
			return (
				f"Error {tool_name}: exceeded {int(TOOL_TOTAL_TIMEOUT_S)}s total; "
				f"browser still reachable — safe to retry; call restart_browser only if calls keep stalling."
			)
		except Exception:
			if tool_name not in _CACHED_READ_TOOLS and hasattr(session, "mark_cdp_error"):
				session.mark_cdp_error()
			raise
	return wrapper
HEADLESS = os.environ.get("STEALTH_BROWSER_HEADLESS", "false").lower() in ("1", "true", "yes")
CHROMIUM_APP_BIN = "/Applications/Chromium.app/Contents/MacOS/Chromium"
DEFAULT_BROWSER_EXECUTABLE = CHROMIUM_APP_BIN if os.path.exists(CHROMIUM_APP_BIN) else None
BROWSER_EXECUTABLE = os.environ.get("STEALTH_BROWSER_EXECUTABLE", DEFAULT_BROWSER_EXECUTABLE)
APPLE_PASSWORDS_EXT_ID = "pejdijmoenmkgeppbflobdenhhabjlaj"
APPLE_PASSWORDS_EXT_DIR = os.path.expanduser(
	os.environ.get("STEALTH_BROWSER_EXTENSIONS_DIR", "~/.local/share/stealth-browser/extensions")
)
APPLE_PASSWORDS_CRX_URL = (
	"https://clients2.google.com/service/update2/crx?response=redirect"
	"&prodversion={prodversion}&acceptformat=crx2,crx3&x=id%3D{ext_id}%26uc"
)
APPLE_PASSWORDS_CRX_PRODVERSION = os.environ.get("STEALTH_BROWSER_CRX_PRODVERSION", "140.0.0.0")
_ID_ALPHABET = "abcdefghijklmnop"
_apple_passwords_ext_cache: Optional[str] = None


def _extension_id_from_key(key_b64: str) -> str:
	"""Derive the Chromium extension ID from a manifest `key` (SHA-256 of DER key, mapped 0-f -> a-p)."""
	digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
	return "".join(_ID_ALPHABET[int(chunk, 16)] for chunk in digest)


def _read_extension_id(ext_dir: str) -> Optional[str]:
	"""Return the extension ID declared by an unpacked extension, or None if unreadable."""
	try:
		with open(os.path.join(ext_dir, "manifest.json"), "r", encoding="utf-8") as f:
			key = json.load(f).get("key")
		return _extension_id_from_key(key) if key else None
	except Exception:
		return None


def _cached_apple_passwords_ext() -> Optional[str]:
	"""Return an already-extracted, identity-verified copy from the tool-owned extension cache."""
	ext_root = os.path.join(APPLE_PASSWORDS_EXT_DIR, "apple-passwords")
	if not os.path.isdir(ext_root):
		return None
	for entry in sorted(os.listdir(ext_root), reverse=True):
		candidate = os.path.join(ext_root, entry)
		if os.path.isfile(os.path.join(candidate, "manifest.json")) and _read_extension_id(candidate) == APPLE_PASSWORDS_EXT_ID:
			return candidate
	return None


def _read_varint(data: bytes, offset: int) -> tuple:
	result = 0
	shift = 0
	while True:
		byte = data[offset]
		offset += 1
		result |= (byte & 0x7F) << shift
		if not byte & 0x80:
			return result, offset
		shift += 7


def _iter_protobuf_fields(data: bytes):
	"""Yield (field_number, value) for a minimal protobuf message; only wire types 0/1/2/5."""
	offset = 0
	while offset < len(data):
		tag, offset = _read_varint(data, offset)
		field, wire = tag >> 3, tag & 7
		if wire == 0:
			value, offset = _read_varint(data, offset)
		elif wire == 2:
			length, offset = _read_varint(data, offset)
			value, offset = data[offset:offset + length], offset + length
		elif wire == 5:
			value, offset = data[offset:offset + 4], offset + 4
		elif wire == 1:
			value, offset = data[offset:offset + 8], offset + 8
		else:
			raise ValueError(f"unsupported protobuf wire type {wire}")
		if not isinstance(value, bytes):
			continue
		yield field, value


def _crx_id_from_pubkey(pubkey: bytes) -> str:
	digest = hashlib.sha256(pubkey).digest()[:16]
	return "".join(_ID_ALPHABET[nibble] for byte in digest for nibble in (byte >> 4, byte & 0xF))


def _rsa_sha256_verify(pubkey_der: bytes, message: bytes, signature: bytes) -> bool:
	"""Verify an RSASSA-PKCS1-v1_5 SHA-256 signature via the openssl CLI (no extra Python deps)."""
	with tempfile.TemporaryDirectory() as tmp:
		pub_path = os.path.join(tmp, "pub.der")
		sig_path = os.path.join(tmp, "sig.bin")
		msg_path = os.path.join(tmp, "msg.bin")
		for path, blob in ((pub_path, pubkey_der), (sig_path, signature), (msg_path, message)):
			with open(path, "wb") as f:
				f.write(blob)
		proc = subprocess.run(
			["openssl", "dgst", "-sha256", "-keyform", "DER", "-verify", pub_path, "-signature", sig_path, msg_path],
			capture_output=True,
			text=True,
		)
	return proc.returncode == 0 and "Verified OK" in proc.stdout


def _verify_crx(data: bytes, expected_id: str) -> Optional[bytes]:
	"""Parse a CRX3 container, verify its publisher-key signature, and return the zip payload.

	Per Chromium's crx3.proto, each sha256_with_rsa proof (field 2) signs
	"CRX3 SignedData\\0" + LE32(signed_header_data size) + signed_header_data (field 10000)
	+ the zip archive. The publisher proof's public key must derive the expected ID.
	"""
	if data[:4] != b"Cr24":
		raise ValueError("not a CRX container")
	version = struct.unpack("<I", data[4:8])[0]
	if version != 3:
		raise ValueError(f"unsupported CRX version {version} (expected 3)")
	header_size = struct.unpack("<I", data[8:12])[0]
	header = data[12:12 + header_size]
	payload = data[12 + header_size:]

	signed_header_data = None
	proofs = []
	for field, value in _iter_protobuf_fields(header):
		if field == 2:
			proofs.append(value)
		elif field == 10000:
			signed_header_data = value
	if signed_header_data is None:
		raise ValueError("CRX header is missing signed_header_data")

	message = (
		b"CRX3 SignedData\x00"
		+ struct.pack("<I", len(signed_header_data))
		+ signed_header_data
		+ payload
	)
	for proof in proofs:
		pubkey = sig = None
		for sub_field, value in _iter_protobuf_fields(proof):
			if sub_field == 1:
				pubkey = value
			elif sub_field == 2:
				sig = value
		if not pubkey or not sig:
			return None
		if _crx_id_from_pubkey(pubkey) != expected_id:
			continue  # not the publisher key we pin; keep looking
		if not _rsa_sha256_verify(pubkey, message, sig):
			return None
		return payload
	raise ValueError("no publisher signature for the expected extension ID in CRX header")


def _download_apple_passwords_crx() -> Optional[bytes]:
	url = APPLE_PASSWORDS_CRX_URL.format(
		prodversion=APPLE_PASSWORDS_CRX_PRODVERSION, ext_id=APPLE_PASSWORDS_EXT_ID
	)
	try:
		req = urllib.request.Request(url, headers={"User-Agent": f"Chromium/{APPLE_PASSWORDS_CRX_PRODVERSION}"})
		with urllib.request.urlopen(req, timeout=15) as resp:
			return resp.read()
	except Exception as exc:
		print(f"stealth-browser: Apple Passwords CRX download failed: {exc}", file=sys.stderr)
		return None


def _install_apple_passwords_ext() -> Optional[str]:
	"""Fetch, identity-check, and unpack the Apple Passwords extension into the tool-owned cache dir."""
	global _apple_passwords_ext_cache
	cached = _cached_apple_passwords_ext()
	if cached:
		_apple_passwords_ext_cache = cached
		return cached

	data = _download_apple_passwords_crx()
	if not data:
		return None
	try:
		payload = _verify_crx(data, APPLE_PASSWORDS_EXT_ID)
	except Exception as exc:
		print(f"stealth-browser: Apple Passwords CRX rejected: {exc}", file=sys.stderr)
		return None

	ext_root = os.path.join(APPLE_PASSWORDS_EXT_DIR, "apple-passwords")
	os.makedirs(ext_root, exist_ok=True)
	staging = tempfile.mkdtemp(prefix=".staging-", dir=ext_root)
	try:
		with zipfile.ZipFile(io.BytesIO(payload)) as archive:
			archive.extractall(staging)
		if _read_extension_id(staging) != APPLE_PASSWORDS_EXT_ID:
			print("stealth-browser: Apple Passwords CRX failed identity check; ignoring", file=sys.stderr)
			return None
		with open(os.path.join(staging, "manifest.json"), "r", encoding="utf-8") as f:
			version = json.load(f).get("version") or "unknown"
		target = os.path.join(ext_root, version)
		if os.path.exists(target):
			shutil.rmtree(target, ignore_errors=True)
		os.replace(staging, target)
		_apple_passwords_ext_cache = target
		return target
	except Exception as exc:
		print(f"stealth-browser: Apple Passwords install failed: {exc}", file=sys.stderr)
		return None
	finally:
		if os.path.isdir(staging):
			shutil.rmtree(staging, ignore_errors=True)

MEDIA_RESOURCE_TYPES = {"image", "media", "font"}
MEDIA_FILE_EXTENSIONS = (
	".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
	".mp4", ".webm", ".ogg", ".woff", ".woff2", ".ttf", ".otf"
)
MEDIA_BLOCK_URL_PATTERNS = tuple(
	pat for ext in MEDIA_FILE_EXTENSIONS for pat in (f"*{ext}", f"*{ext}?*")
)
MEDIA_FETCH_PATTERNS = (
	{"resourceType": "Image"},
	{"resourceType": "Media"},
	{"resourceType": "Font"},
)
BLOCK_TRACKERS = os.environ.get("STEALTH_BROWSER_BLOCK_TRACKERS", "true").lower() in ("1", "true", "yes")
TRACKER_BLOCK_URL_PATTERNS = (
	"*://*.google-analytics.com/*",
	"*://*.googletagmanager.com/*",
	"*://*.doubleclick.net/*",
	"*://*.googlesyndication.com/*",
	"*://*.googleadservices.com/*",
	"*://*.connect.facebook.net/*",
	"*://*.facebook.com/tr*",
	"*://*.hotjar.com/*",
	"*://*.hotjar.io/*",
	"*://*.segment.io/*",
	"*://*.segment.com/*",
	"*://*.amplitude.com/*",
	"*://*.mixpanel.com/*",
	"*://*.fullstory.com/*",
	"*://*.clarity.ms/*",
	"*://*.nr-data.net/*",
	"*://*.newrelic.com/*",
	"*://*.datadoghq-browser-agent.com/*",
	"*://*.browser-intake-datadoghq.com/*",
	"*://*.bugsnag.com/*",
	"*://*.optimizely.com/*",
	"*://*.hs-analytics.net/*",
	"*://*.hs-scripts.com/*",
	"*://*.hs-banner.com/*",
	"*://*.ads-twitter.com/*",
	"*://*.analytics.tiktok.com/*",
	"*://*.bat.bing.com/*",
	"*://*.criteo.com/*",
	"*://*.criteo.net/*",
	"*://*.taboola.com/*",
	"*://*.outbrain.com/*",
	"*://*.quantserve.com/*",
	"*://*.scorecardresearch.com/*",
	"*://*.adnxs.com/*",
	"*://*.rubiconproject.com/*",
	"*://*.pubmatic.com/*",
	"*://*.openx.net/*",
	"*://*.demdex.net/*",
	"*://*.omtrdc.net/*",
	"*://*.2o7.net/*",
	"*://*.branch.io/*",
	"*://*.braze.com/*",
	"*://*.chartbeat.com/*",
	"*://*.chartbeat.net/*",
	"*://*.mouseflow.com/*",
	"*://*.crazyegg.com/*",
	"*://*.heapanalytics.com/*",
	"*://*.pendo.io/*",
	"*://*.adservice.google.com/*",
	"*://*.amazon-adsystem.com/*",
	"*://*.bing.com/bat.js*",
	"*://*.linkedin.com/px/*",
	"*://*.snap.licdn.com/*",
	"*://*.pinterest.com/ct/*",
	"*://*.ct.pinterest.com/*",
)

CACHE_DIR = os.path.expanduser("~/.cache/stealth-browser")
STATE_FILE = os.path.join(CACHE_DIR, f"state-{CDP_PORT}.json")


def _load_persisted_headless() -> bool:
	"""Return persisted headless preference if STEALTH_BROWSER_HEADLESS is not explicitly set."""
	if "STEALTH_BROWSER_HEADLESS" in os.environ:
		return HEADLESS
	try:
		if os.path.exists(STATE_FILE):
			with open(STATE_FILE, "r", encoding="utf-8") as f:
				data = json.load(f)
			if isinstance(data.get("headless"), bool):
				return data["headless"]
	except Exception:
		pass
	return HEADLESS


def _save_persisted_headless(headless: Optional[bool]) -> None:
	"""Persist headless mode for crash recovery, or remove state file when None."""
	try:
		if headless is None:
			if os.path.exists(STATE_FILE):
				os.unlink(STATE_FILE)
			return
		os.makedirs(CACHE_DIR, exist_ok=True)
		with open(STATE_FILE, "w", encoding="utf-8") as f:
			json.dump({"headless": bool(headless)}, f)
	except Exception:
		pass


ANIMATIONS_STATE_FILE = os.path.join(CACHE_DIR, f"animations-{CDP_PORT}.json")
_ANIMATIONS_STYLE_ATTR = "data-anim-pref"
_ANIMATIONS_KILL_CSS = (
	"*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important; "
	"transition-duration: 0.001s !important; transition-delay: 0s !important; scroll-behavior: auto !important; }"
)
_ANIMATIONS_INJECT_JS = """(css) => {
	let style = document.querySelector('style[' + %(attr)s + ']');
	if (!style) {
		style = document.createElement('style');
		style.setAttribute(%(attr)s, '1');
		(document.head || document.documentElement).appendChild(style);
	}
	style.textContent = css;
}""" % {"attr": json.dumps(_ANIMATIONS_STYLE_ATTR)}
_ANIMATIONS_INIT_SCRIPT = """(() => {
	const css = %(css)s;
	const inject = () => {
		if (!document.documentElement) return;
		const style = document.createElement('style');
		style.setAttribute(%(attr)s, 'init');
		style.textContent = css;
		(document.head || document.documentElement).appendChild(style);
	};
	if (document.documentElement) inject();
	else document.addEventListener('DOMContentLoaded', inject, { once: true });
})();""" % {"css": json.dumps(_ANIMATIONS_KILL_CSS), "attr": json.dumps(_ANIMATIONS_STYLE_ATTR)}


def _load_persisted_animations() -> Optional[bool]:
	"""Return persisted animation-freeze preference, or None when never set."""
	try:
		if os.path.exists(ANIMATIONS_STATE_FILE):
			with open(ANIMATIONS_STATE_FILE, "r", encoding="utf-8") as f:
				data = json.load(f)
			if isinstance(data.get("animations"), bool):
				return data["animations"]
	except Exception:
		pass
	return None


def _save_persisted_animations(enabled: Optional[bool]) -> None:
	"""Persist animation-freeze state for crash recovery, or remove state file when None."""
	try:
		if enabled is None:
			if os.path.exists(ANIMATIONS_STATE_FILE):
				os.unlink(ANIMATIONS_STATE_FILE)
			return
		os.makedirs(CACHE_DIR, exist_ok=True)
		with open(ANIMATIONS_STATE_FILE, "w", encoding="utf-8") as f:
			json.dump({"animations": bool(enabled)}, f)
	except Exception:
		pass


def _acquire_file_lock(lock_file, timeout_s: float = 15.0) -> None:
	deadline = time.monotonic() + timeout_s
	while True:
		try:
			fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
			return
		except BlockingIOError:
			if time.monotonic() >= deadline:
				raise TimeoutError(f"Timed out acquiring file lock after {timeout_s}s")
			time.sleep(0.05)


def _resolve_apple_passwords_ext_locked() -> Optional[str]:
	os.makedirs(CACHE_DIR, exist_ok=True)
	with open(os.path.join(CACHE_DIR, "apple-passwords-ext.lock"), "w", encoding="utf-8") as lock_file:
		_acquire_file_lock(lock_file, timeout_s=15.0)
		try:
			return _install_apple_passwords_ext()
		finally:
			fcntl.flock(lock_file, fcntl.LOCK_UN)


class NetworkBlockManager:
	"""Applies per-page CDP media and tracker blocking without disabling Chromium's HTTP cache."""

	def __init__(self, block_trackers: Optional[bool] = None):
		self._block_trackers_override = block_trackers
		self._sessions: dict[Page, Any] = {}
		self._media_enabled: set[Page] = set()
		self._pending_tasks: set[asyncio.Task] = set()
		self._inflight_pages: set[Page] = set()

	@property
	def block_trackers(self) -> bool:
		return BLOCK_TRACKERS if self._block_trackers_override is None else self._block_trackers_override

	def _build_blocked_urls(self, block_media: bool) -> list[str]:
		urls: list[str] = []
		if self.block_trackers:
			urls.extend(TRACKER_BLOCK_URL_PATTERNS)
		if block_media:
			urls.extend(MEDIA_BLOCK_URL_PATTERNS)
		return urls

	async def _fail_request(self, cdp: Any, request_id: str) -> None:
		try:
			await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
		except Exception:
			pass

	async def _ensure_session(self, page: Page) -> Any:
		if page is None or page.is_closed():
			return None
		existing = self._sessions.get(page)
		if existing is not None:
			return existing
		context = getattr(page, "context", None)
		if context is None or not hasattr(context, "new_cdp_session"):
			return None
		try:
			cdp = await asyncio.wait_for(context.new_cdp_session(page), timeout=CDP_OP_TIMEOUT_S)
		except Exception:
			return None
		try:
			await asyncio.wait_for(
				cdp.send("Network.enable", {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0}),
				timeout=CDP_OP_TIMEOUT_S,
			)
		except Exception:
			await self._detach_quiet(cdp)
			return None

		def _on_paused(ev: Any) -> None:
			req_id = ev.get("requestId") if isinstance(ev, dict) else None
			if not req_id:
				return
			try:
				loop = asyncio.get_running_loop()
			except RuntimeError:
				return
			task = loop.create_task(self._fail_request(cdp, req_id))
			self._pending_tasks.add(task)
			task.add_done_callback(self._pending_tasks.discard)

		cdp.on("Fetch.requestPaused", _on_paused)
		self._sessions[page] = cdp
		if not getattr(page, "_network_blocker_close_hook", False):
			setattr(page, "_network_blocker_close_hook", True)
			page.on("close", lambda _=None, p=page: self._on_close(p))
			page.on("crash", lambda _=None, p=page: self._on_crash(p))
		return cdp

	def schedule_ensure_page(self, page: Page) -> None:
		if page is None or page in self._sessions or page in self._inflight_pages:
			return
		try:
			if page.is_closed():
				return
		except Exception:
			return
		block_media = bool(getattr(page, "_media_blocked", False))
		if not block_media and not self.block_trackers:
			return
		try:
			loop = asyncio.get_running_loop()
		except RuntimeError:
			return
		self._inflight_pages.add(page)
		task = loop.create_task(self.ensure_page(page))
		self._pending_tasks.add(task)

		def _done(t: asyncio.Task, p: Page = page) -> None:
			self._pending_tasks.discard(t)
			self._inflight_pages.discard(p)

		task.add_done_callback(_done)

	async def ensure_page(self, page: Page) -> bool:
		if page is None:
			return False
		try:
			if page.is_closed():
				return False
		except Exception:
			return False
		block_media = bool(getattr(page, "_media_blocked", False))
		if not block_media and not self.block_trackers:
			return False
		if page in self._sessions and (page in self._media_enabled) == block_media:
			return True
		return await self.set_media_blocked(page, block_media)

	async def set_media_blocked(self, page: Page, block_media: bool) -> bool:
		if page is None:
			return False
		try:
			if page.is_closed():
				return False
		except Exception:
			return False

		if not block_media and not self.block_trackers:
			cdp = self._sessions.pop(page, None)
			was_media = page in self._media_enabled
			self._media_enabled.discard(page)
			if cdp is not None:
				if was_media:
					try:
						await asyncio.wait_for(cdp.send("Fetch.disable"), timeout=CDP_OP_TIMEOUT_S)
					except Exception:
						pass
				try:
					await asyncio.wait_for(cdp.send("Network.setBlockedURLs", {"urls": []}), timeout=CDP_OP_TIMEOUT_S)
				except Exception:
					pass
				await self._detach_quiet(cdp)
			return True

		cdp = await self._ensure_session(page)
		if cdp is None:
			return False
		urls = self._build_blocked_urls(block_media)
		try:
			await asyncio.wait_for(cdp.send("Network.setBlockedURLs", {"urls": urls}), timeout=CDP_OP_TIMEOUT_S)
		except Exception:
			return False
		if block_media and page not in self._media_enabled:
			try:
				await asyncio.wait_for(
					cdp.send("Fetch.enable", {"patterns": list(MEDIA_FETCH_PATTERNS)}),
					timeout=CDP_OP_TIMEOUT_S,
				)
				self._media_enabled.add(page)
			except Exception:
				pass
		elif not block_media and page in self._media_enabled:
			try:
				await asyncio.wait_for(cdp.send("Fetch.disable"), timeout=CDP_OP_TIMEOUT_S)
			except Exception:
				pass
			self._media_enabled.discard(page)
		return True

	@staticmethod
	async def _detach_quiet(cdp: Any) -> None:
		try:
			await asyncio.wait_for(cdp.detach(), timeout=CDP_OP_TIMEOUT_S)
		except Exception:
			pass

	@staticmethod
	def _schedule_detach(cdp: Any) -> None:
		try:
			asyncio.get_running_loop().create_task(NetworkBlockManager._detach_quiet(cdp))
		except Exception:
			pass

	def _on_close(self, page: Page) -> None:
		cdp = self._sessions.pop(page, None)
		self._media_enabled.discard(page)
		if cdp is not None:
			self._schedule_detach(cdp)

	def _on_crash(self, page: Page) -> None:
		self._sessions.pop(page, None)
		self._media_enabled.discard(page)

	async def close_all(self) -> None:
		tasks = list(self._pending_tasks)
		self._pending_tasks.clear()
		for task in tasks:
			if not task.done():
				task.cancel()
		if tasks:
			await asyncio.gather(*tasks, return_exceptions=True)
		sessions = list(self._sessions.values())
		self._sessions.clear()
		self._media_enabled.clear()
		self._inflight_pages.clear()
		if sessions:
			await asyncio.gather(*(self._detach_quiet(cdp) for cdp in sessions), return_exceptions=True)


network_blocker = NetworkBlockManager()


_in_lifecycle_op: contextvars.ContextVar[bool] = contextvars.ContextVar("_in_lifecycle_op", default=False)


def _session_key(ctx: Optional[Context] = None) -> str:
	if ctx is None:
		return "default"
	try:
		req_ctx = getattr(ctx, "request_context", None)
		req = getattr(req_ctx, "request", None) if req_ctx is not None else None
		headers = getattr(req, "headers", None) if req is not None else None
		if headers is not None:
			sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
			if sid:
				return f"http:{sid}"
	except Exception:
		pass
	try:
		cid = getattr(ctx, "client_id", None)
		if cid:
			return f"client:{cid}"
	except Exception:
		pass
	try:
		sess = getattr(ctx, "session", None)
		if sess is not None:
			key = f"session:{id(sess)}"
			session.register_client_session(key, sess)
			return key
	except Exception:
		pass
	return "default"


async def _resolve_page(tab: Union[int, str] = -1, ctx: Optional[Context] = None) -> Page:
	client_key = _session_key(ctx)
	if client_key == "default":
		return await session.resolve_page(tab)
	return await session.resolve_page(tab, client_key=client_key)


def _tab_id_from_target(target_id: str) -> str:
	return "t" + re.sub(r"[^0-9a-f]", "", target_id.lower())[:10]


def _tab_name_from_title(title: str) -> str:
	slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40]
	return slug or "untitled"


async def _cdp_target_id(page: Page) -> Optional[str]:
	cached = getattr(page, "_stealth_target_id", None)
	if isinstance(cached, str) and cached:
		return cached
	context = getattr(page, "context", None)
	if context is None or not hasattr(context, "new_cdp_session"):
		return None
	try:
		cdp = await asyncio.wait_for(context.new_cdp_session(page), timeout=CDP_OP_TIMEOUT_S)
		info = await asyncio.wait_for(cdp.send("Target.getTargetInfo"), timeout=CDP_OP_TIMEOUT_S)
		target_id = (info or {}).get("targetInfo", {}).get("targetId")
		if isinstance(target_id, str) and target_id.strip():
			value = target_id.strip()
			setattr(page, "_stealth_target_id", value)
			return value
	except Exception:
		pass
	return None


async def _resolve_tab_name(manager, pages: list[Page], name: str) -> int:
	matched = -1
	for idx, page in enumerate(pages):
		try:
			title = await asyncio.wait_for(page.title(), timeout=PAGE_TITLE_TIMEOUT_S)
		except Exception:
			continue
		if _tab_name_from_title(title) == name or name in title.lower():
			if matched >= 0:
				return -1
			matched = idx
	return matched


class BrowserSessionManager:
	"""Manages dedicated stealth Chromium browser or CDP connection for AI agents."""

	def __init__(self, cdp_url: str):
		self.cdp_url = cdp_url
		self.headless: bool = _load_persisted_headless()
		self.animations: Optional[bool] = _load_persisted_animations()
		self._playwright = None
		self._browser: Optional[Browser] = None
		self._context: Optional[BrowserContext] = None
		self._active_pages: dict[str, Page] = {}
		self._client_finalizers: dict[str, weakref.finalize] = {}
		self._connect_lock: asyncio.Lock = asyncio.Lock()
		self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
		self._idle_task: Optional[asyncio.Task] = None
		self._page_to_id: dict[Page, str] = {}
		self._id_to_page: dict[str, Page] = {}
		self._next_tab_seq: int = 1
		self._last_cdp_ok_at: float = 0.0
		self._last_cdp_error_at: float = 0.0
		self._probe_task: Optional[asyncio.Task] = None
		self._recovery_gen: int = 0

	def mark_cdp_ok(self) -> None:
		self._last_cdp_ok_at = time.monotonic()

	def mark_cdp_error(self) -> None:
		self._last_cdp_error_at = time.monotonic()

	def _needs_cdp_probe(self) -> bool:
		if self._context is None:
			return False
		if self._last_cdp_error_at > self._last_cdp_ok_at:
			return True
		if self._last_cdp_ok_at > 0.0 and (time.monotonic() - self._last_cdp_ok_at) >= CDP_PROBE_STALE_S:
			return True
		return False

	async def _run_two_strike_probe(self) -> bool:
		for attempt in range(2):
			try:
				ok = await asyncio.wait_for(asyncio.to_thread(self._probe_cdp_ready), timeout=2.0)
			except Exception:
				ok = False
			if ok:
				self.mark_cdp_ok()
				return True
			if attempt == 0:
				await asyncio.sleep(CDP_PROBE_RETRY_SLEEP_S)
		return False

	async def _recover_wedged_cdp(self) -> bool:
		gen_before = self._recovery_gen
		async with self._lifecycle_lock:
			if self._recovery_gen != gen_before:
				return False
			try:
				if await asyncio.wait_for(asyncio.to_thread(self._probe_cdp_ready), timeout=2.0):
					self.mark_cdp_ok()
					return False
			except Exception:
				pass
			try:
				await asyncio.wait_for(self.close(), timeout=5.0)
			except Exception:
				pass
			try:
				await asyncio.wait_for(asyncio.to_thread(_force_kill_browser_processes), timeout=10.0)
			except Exception:
				pass
			self._recovery_gen += 1
			self._last_cdp_ok_at = 0.0
			self._last_cdp_error_at = 0.0
			return True

	async def ensure_cdp_healthy(self) -> bool:
		if not self._needs_cdp_probe():
			return True
		task = self._probe_task
		if task is None or task.done():
			task = asyncio.create_task(self._run_two_strike_probe())
			self._probe_task = task
		try:
			ok = await asyncio.shield(task)
		finally:
			if self._probe_task is task and task.done():
				self._probe_task = None
		if not ok:
			await self._recover_wedged_cdp()
			return False
		return True

	@property
	def _active_page(self) -> Optional[Page]:
		return self._active_pages.get("default")

	@_active_page.setter
	def _active_page(self, page: Optional[Page]) -> None:
		if page is None:
			self._active_pages.pop("default", None)
		else:
			self._active_pages["default"] = page

	def register_client_session(self, client_key: str, session_obj: Any) -> None:
		if client_key == "default" or client_key in self._client_finalizers:
			return
		try:
			def _cleanup(_key=client_key):
				self._active_pages.pop(_key, None)
				self._client_finalizers.pop(_key, None)

			self._client_finalizers[client_key] = weakref.finalize(session_obj, _cleanup)
		except TypeError:
			pass

	def clear_client(self, client_key: str) -> None:
		self._active_pages.pop(client_key, None)
		fin = self._client_finalizers.pop(client_key, None)
		if fin is not None:
			fin.detach()

	def get_active_page_for(self, client_key: str = "default") -> Optional[Page]:
		page = self._active_pages.get(client_key)
		if page is None:
			return None
		try:
			if page.is_closed():
				self._active_pages.pop(client_key, None)
				return None
		except Exception:
			self._active_pages.pop(client_key, None)
			return None
		return page

	def sibling_active_tabs(self, client_key: str = "default") -> list[tuple[str, str, Page, bool]]:
		self._prune_closed_tabs()
		seen: dict[tuple[str, Page], bool] = {}
		for other_key, page in list(self._active_pages.items()):
			if other_key == client_key or page is None:
				continue
			try:
				if page.is_closed():
					self._active_pages.pop(other_key, None)
					continue
			except Exception:
				self._active_pages.pop(other_key, None)
				continue
			ka_active = "keep_alive_mgr" in globals() and keep_alive_mgr.is_active(page)
			seen[(other_key, page)] = ka_active
		if "keep_alive_mgr" in globals():
			for page, owner in keep_alive_mgr.active_pages_with_owners():
				if owner == client_key:
					continue
				seen[(owner, page)] = True
		results: list[tuple[str, str, Page, bool]] = []
		for (other_key, page), ka_active in seen.items():
			results.append((other_key, self.get_tab_id(page), page, ka_active))
		return results

	def _touch_activity(self):
		if self._idle_task is not None and not self._idle_task.done():
			self._idle_task.cancel()
		try:
			loop = asyncio.get_running_loop()
			self._idle_task = loop.create_task(self._idle_watchdog())
		except RuntimeError:
			pass

	async def _idle_watchdog(self):
		try:
			await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
			if "keep_alive_mgr" in globals() and keep_alive_mgr.has_active():
				self._touch_activity()
				return
			if self._browser is not None or self._context is None:
				await self.close()
		except asyncio.CancelledError:
			pass

	async def close(self):
		"""Cleanly stop the patchright node driver and release browser handles."""
		async with self._connect_lock:
			if "keep_alive_mgr" in globals():
				try:
					await asyncio.wait_for(keep_alive_mgr.stop_all(), timeout=CDP_OP_TIMEOUT_S)
				except Exception:
					pass
			if "network_blocker" in globals():
				try:
					await asyncio.wait_for(network_blocker.close_all(), timeout=CDP_OP_TIMEOUT_S)
				except Exception:
					pass
			if (
				self._idle_task is not None
				and not self._idle_task.done()
				and asyncio.current_task() is not self._idle_task
			):
				self._idle_task.cancel()
			self._idle_task = None
			self._context = None
			self._browser = None
			self._active_pages.clear()
			self._page_to_id.clear()
			self._id_to_page.clear()
			if self._playwright is not None:
				try:
					await asyncio.wait_for(self._playwright.stop(), timeout=PLAYWRIGHT_STOP_TIMEOUT_S)
				except Exception:
					pass
				finally:
					self.close_sync()

	def close_sync(self):
		"""Synchronously terminate the patchright node driver subprocess if still alive."""
		if self._playwright is None:
			return
		try:
			proc = getattr(self._playwright._impl_obj._connection._transport, "_proc", None)
			if proc is not None and getattr(proc, "pid", None):
				os.kill(proc.pid, signal.SIGTERM)
		except Exception:
			pass
		self._playwright = None

	def _detect_cdp_headless(self) -> Optional[bool]:
		"""Detect whether the Chromium instance listening on CDP_PORT is running headless."""
		version_detected: Optional[bool] = None
		try:
			with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=0.5) as resp:
				data = json.loads(resp.read().decode("utf-8", errors="ignore"))
			ua = data.get("User-Agent", "")
			browser_str = data.get("Browser", "")
			if ua or browser_str:
				version_detected = "HeadlessChrome" in ua or "HeadlessChrome" in browser_str
				if version_detected:
					return True
		except Exception:
			pass
		try:
			pid_res = subprocess.run(
				["lsof", "-nP", f"-iTCP:{CDP_PORT}", "-sTCP:LISTEN", "-t"],
				capture_output=True,
				text=True,
				timeout=1.5,
			)
			pids = [p.strip() for p in pid_res.stdout.splitlines() if p.strip()]
			if pids:
				ps_res = subprocess.run(
					["ps", "-ww", "-o", "args=", "-p", pids[0]],
					capture_output=True,
					text=True,
					timeout=1.5,
				)
				args = ps_res.stdout.strip()
				if args:
					return "--headless" in args
		except Exception:
			pass
		return version_detected

	async def _connect_cdp(self) -> Optional[BrowserContext]:
		"""Attach to an already-running Chromium CDP endpoint and sync headless state."""
		try:
			self._browser = await asyncio.wait_for(
				self._playwright.chromium.connect_over_cdp(
					self.cdp_url,
					timeout=1500,
				),
				timeout=CDP_CONNECT_TIMEOUT_S,
			)

			async def _detect_headless():
				try:
					return await asyncio.wait_for(
						asyncio.to_thread(self._detect_cdp_headless),
						timeout=CDP_OP_TIMEOUT_S,
					)
				except Exception:
					return None

			ctx = self._browser.contexts[0] if self._browser.contexts else None
			if ctx is not None:
				self._context = ctx
				detected_headless, _ = await asyncio.gather(
					_detect_headless(),
					self._ensure_interceptor(ctx),
				)
				if detected_headless is not None:
					self.headless = detected_headless
				return self._context
			detected_headless = await _detect_headless()
			if detected_headless is not None:
				self.headless = detected_headless
		except Exception:
			self._browser = None
		return None

	def _probe_cdp_ready(self) -> bool:
		try:
			with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=0.3) as resp:
				return resp.status == 200
		except Exception:
			return False

	async def _launch_detached_chromium(self) -> None:
		"""Launch standalone Chromium detached from the MCP server process so it survives restarts. Headed launches go through open -g so the window never takes focus."""
		os.makedirs(PROFILE_DIR, exist_ok=True)

		nm_hosts_dir = os.path.join(PROFILE_DIR, "NativeMessagingHosts")
		os.makedirs(nm_hosts_dir, exist_ok=True)
		nm_manifest = os.path.join(nm_hosts_dir, "com.apple.passwordmanager.json")
		system_manifest = "/Library/Google/Chrome/NativeMessagingHosts/com.apple.passwordmanager.json"
		if not os.path.exists(nm_manifest) and os.path.exists(system_manifest):
			shutil.copy2(system_manifest, nm_manifest)

		executable = BROWSER_EXECUTABLE or self._playwright.chromium.executable_path
		browser_args = [
			f"--remote-debugging-port={CDP_PORT}",
			f"--user-data-dir={PROFILE_DIR}",
			"--test-type",
			"--no-first-run",
			"--no-default-browser-check",
			"--disable-search-engine-choice-screen",
			"--disable-sync",
			"--disable-background-networking",
			"--disable-background-timer-throttling",
			"--disable-backgrounding-occluded-windows",
			"--disable-renderer-backgrounding",
			"--disable-prompt-on-repost",
			"--use-mock-keychain",
			"--disable-blink-features=AutomationControlled",
		]
		if self.headless:
			browser_args.append("--headless=new")
		else:
			browser_args.append("--no-startup-window")
		apple_passwords_ext = _apple_passwords_ext_cache
		if not apple_passwords_ext:
			try:
				apple_passwords_ext = await asyncio.wait_for(
					asyncio.to_thread(_resolve_apple_passwords_ext_locked),
					timeout=20.0,
				)
			except Exception as exc:
				print(f"stealth-browser: Apple Passwords setup skipped: {exc}", file=sys.stderr)
				apple_passwords_ext = None
		if apple_passwords_ext:
			browser_args.extend([
				f"--disable-extensions-except={apple_passwords_ext}",
				f"--load-extension={apple_passwords_ext}",
			])

		proc: Optional[subprocess.Popen] = None
		if self.headless:
			proc = subprocess.Popen(
				[executable, *browser_args],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				start_new_session=True,
			)
		else:
			app_bundle = re.match(r"^(.*\.app)", executable)
			if app_bundle:
				launched = subprocess.run(
					["open", "-g", "-n", "-a", app_bundle.group(1), "--args", *browser_args],
					stdin=subprocess.DEVNULL,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
					timeout=10,
				)
				if launched.returncode != 0:
					raise RuntimeError(f"Background headed launch failed for {app_bundle.group(1)} (exit {launched.returncode})")
			else:
				proc = subprocess.Popen(
					[executable, *browser_args],
					stdin=subprocess.DEVNULL,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
					start_new_session=True,
				)

		deadline = time.monotonic() + 5.0
		while time.monotonic() < deadline:
			if proc is not None and proc.poll() is not None:
				raise RuntimeError(f"Detached Chromium exited early with code {proc.returncode}")
			if await asyncio.to_thread(self._probe_cdp_ready):
				return
			await asyncio.sleep(0.05)
		raise TimeoutError(f"Timed out waiting for Chromium CDP endpoint at {self.cdp_url}")

	async def get_context(self) -> BrowserContext:
		self._touch_activity()
		async with self._connect_lock:
			if self._playwright is None:
				self._playwright = await asyncio.wait_for(
					async_playwright().start(),
					timeout=PLAYWRIGHT_START_TIMEOUT_S,
				)

			if self._context is not None:
				try:
					if self._browser is not None and not self._browser.is_connected():
						self._browser = None
						self._context = None
					elif hasattr(self._context, "is_closed") and self._context.is_closed():
						self._context = None
					else:
						_ = self._context.pages
						return self._context
				except Exception:
					self._context = None
					self._browser = None

			ctx = await self._connect_cdp()
			if ctx is not None:
				self.mark_cdp_ok()
				return ctx

			os.makedirs(CACHE_DIR, exist_ok=True)
			lock_path = os.path.join(CACHE_DIR, f"launch-{CDP_PORT}.lock")
			with open(lock_path, "w", encoding="utf-8") as lock_file:
				await asyncio.to_thread(_acquire_file_lock, lock_file, 15.0)
				try:
					ctx = await self._connect_cdp()
					if ctx is not None:
						self.mark_cdp_ok()
						return ctx
					await self._launch_detached_chromium()
					ctx = await self._connect_cdp()
					if ctx is not None:
						self.mark_cdp_ok()
						return ctx
				finally:
					fcntl.flock(lock_file, fcntl.LOCK_UN)

			raise RuntimeError(f"Failed to connect to Chromium CDP endpoint at {self.cdp_url}")

	async def _configure_native_downloads(self, context: BrowserContext, page: Optional[Page] = None):
		"""Override Playwright's allowAndName download interception with native Chromium downloads."""
		if getattr(context, "_downloads_configured", False):
			return
		os.makedirs(DOWNLOADS_DIR, exist_ok=True)
		cdp = None
		try:
			if self._browser is not None:
				cdp = await asyncio.wait_for(
					self._browser.new_browser_cdp_session(),
					timeout=CDP_OP_TIMEOUT_S,
				)
			else:
				target_page = page or (context.pages[0] if context.pages else None)
				if target_page is None or target_page.is_closed():
					return
				cdp = await asyncio.wait_for(
					context.new_cdp_session(target_page),
					timeout=CDP_OP_TIMEOUT_S,
				)
			await asyncio.wait_for(
				cdp.send(
					"Browser.setDownloadBehavior",
					{
						"behavior": "allow",
						"downloadPath": DOWNLOADS_DIR,
					},
				),
				timeout=CDP_OP_TIMEOUT_S,
			)
			setattr(context, "_downloads_configured", True)
		except Exception:
			pass
		finally:
			if cdp is not None:
				try:
					await asyncio.wait_for(cdp.detach(), timeout=CDP_OP_TIMEOUT_S)
				except Exception:
					pass

	async def _ensure_interceptor(self, context: BrowserContext):
		await asyncio.gather(
			self._configure_native_downloads(context),
			self._ensure_animation_init_script(context),
		)
		if context is not None and not getattr(context, "_page_hook_attached", False) and hasattr(context, "on"):
			setattr(context, "_page_hook_attached", True)

			def _on_context_page(page: Page) -> None:
				try:
					self._register_page_listeners(page)
					network_blocker.schedule_ensure_page(page)
				except Exception:
					pass

			try:
				context.on("page", _on_context_page)
			except Exception:
				pass
		setattr(context, "_interceptor_attached", True)

	async def _ensure_animation_init_script(self, context: BrowserContext) -> None:
		"""Install the kill-stylesheet init script on a context when animations are frozen."""
		if self.animations is not False or context is None or getattr(context, "_animations_init_script", False):
			return
		setattr(context, "_animations_init_script", True)
		try:
			await asyncio.wait_for(context.add_init_script(_ANIMATIONS_INIT_SCRIPT), timeout=CDP_OP_TIMEOUT_S)
		except Exception:
			pass

	def _register_page_listeners(self, page: Page):
		"""Attach lifecycle and dialog event listeners to keep cached state clean and prevent modal hangs."""
		if getattr(page, "_agent_listeners_attached", False):
			return
		setattr(page, "_agent_listeners_attached", True)
		dialogs = getattr(page, "_dialog_history", None)
		if dialogs is None:
			dialogs = deque(maxlen=MAX_DIALOG_HISTORY)
			setattr(page, "_dialog_history", dialogs)

		def _on_closed_or_crashed(_=None):
			for k, p in list(self._active_pages.items()):
				if p is page:
					self._active_pages.pop(k, None)

		async def _on_dialog(dialog):
			dialog_type = str(getattr(dialog, "type", "unknown") or "unknown").lower()
			message = str(getattr(dialog, "message", "") or "")
			try:
				dialog_url = page.url or ""
			except Exception:
				dialog_url = ""
			dialogs.append({
				"type": dialog_type,
				"message": message,
				"url": dialog_url,
				"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
			})
			try:
				if dialog_type == "beforeunload":
					await asyncio.wait_for(dialog.accept(), timeout=DIALOG_ACTION_TIMEOUT_S)
				else:
					await asyncio.wait_for(dialog.dismiss(), timeout=DIALOG_ACTION_TIMEOUT_S)
			except Exception:
				pass

		page.on("close", _on_closed_or_crashed)
		page.on("crash", _on_closed_or_crashed)
		page.on("dialog", _on_dialog)
		download_tracker.attach(page)
		telemetry_tracker.attach(page)
		network_blocker.schedule_ensure_page(page)

	def _unregister_page(self, page: Page) -> None:
		tab_id = self._page_to_id.pop(page, None)
		if tab_id is not None and self._id_to_page.get(tab_id) is page:
			self._id_to_page.pop(tab_id, None)
		for k, p in list(self._active_pages.items()):
			if p is page:
				self._active_pages.pop(k, None)

	def _prune_closed_tabs(self) -> None:
		for page in list(self._page_to_id):
			try:
				closed = page.is_closed()
			except Exception:
				closed = True
			if closed:
				self._unregister_page(page)
		for k, page in list(self._active_pages.items()):
			if page is None:
				self._active_pages.pop(k, None)
				continue
			try:
				closed = page.is_closed()
			except Exception:
				closed = True
			if closed:
				self._active_pages.pop(k, None)

	def get_tab_id(self, page: Page) -> str:
		existing = self._page_to_id.get(page)
		if existing is not None:
			return existing
		target_id = getattr(page, "_stealth_target_id", None)
		if isinstance(target_id, str) and target_id:
			tab_id = _tab_id_from_target(target_id)
			previous = self._id_to_page.get(tab_id)
			if previous is not None and previous is not page:
				self._page_to_id.pop(previous, None)
		else:
			tab_id = f"t{self._next_tab_seq}"
			self._next_tab_seq += 1
		self._page_to_id[page] = tab_id
		self._id_to_page[tab_id] = page
		if not getattr(page, "_tab_id_close_attached", False):
			setattr(page, "_tab_id_close_attached", True)
			page.on("close", lambda _=None, p=page: self._unregister_page(p))
		return tab_id

	async def sync_tab_ids(self, pages: Optional[list[Page]] = None) -> None:
		if pages is None:
			if self._playwright is None:
				return
			try:
				pages = await self.get_pages()
			except Exception:
				return
		for page in pages:
			if self._page_to_id.get(page) is not None:
				continue
			await _cdp_target_id(page)
			self.get_tab_id(page)

	def reassign_tab_id(self, page: Page, tab_id: str) -> None:
		norm = str(tab_id).strip().lower()
		if not norm:
			return
		old_id = self._page_to_id.get(page)
		if old_id == norm and self._id_to_page.get(norm) is page:
			return
		if old_id is not None and self._id_to_page.get(old_id) is page:
			self._id_to_page.pop(old_id, None)
		prev_page = self._id_to_page.get(norm)
		if prev_page is not None and prev_page is not page:
			self._page_to_id.pop(prev_page, None)
		self._page_to_id[page] = norm
		self._id_to_page[norm] = page
		match = re.match(r"^t(\d+)$", norm)
		if match:
			self._next_tab_seq = max(self._next_tab_seq, int(match.group(1)) + 1)
		if not getattr(page, "_tab_id_close_attached", False):
			setattr(page, "_tab_id_close_attached", True)
			page.on("close", lambda _=None, p=page: self._unregister_page(p))

	def _format_open_tabs_hint(self, pages: Optional[list[Page]] = None) -> str:
		if pages is None:
			ctx_pages = getattr(self._context, "pages", None) if self._context is not None else None
			if isinstance(ctx_pages, (list, tuple)):
				pages = []
				for p in ctx_pages:
					try:
						if not p.is_closed():
							pages.append(p)
					except Exception:
						continue
			else:
				pages = [p for p in self._id_to_page.values() if p is not None]
		if not pages:
			return ""
		entries = []
		for p in pages[:8]:
			try:
				tid = self.get_tab_id(p)
				url = _truncate_url(getattr(p, "url", "") or "", 80)
				entries.append(f"[{tid}] {url}")
			except Exception:
				continue
		if not entries:
			return ""
		extra = f" (+{len(pages) - len(entries)} more)" if len(pages) > len(entries) else ""
		return f" Open tabs: {', '.join(entries)}{extra}"

	def set_active_page(self, page: Optional[Page], client_key: str = "default"):
		if page is None:
			self._active_pages.pop(client_key, None)
			return
		self._active_pages[client_key] = page
		if not page.is_closed():
			self._register_page_listeners(page)
			self.get_tab_id(page)

	async def get_pages(self) -> list[Page]:
		context = await asyncio.wait_for(self.get_context(), timeout=GET_CONTEXT_TIMEOUT_S)
		self._prune_closed_tabs()
		open_pages = []
		for p in context.pages:
			try:
				if not p.is_closed():
					self._register_page_listeners(p)
					_attach_page_trackers(p)
					self.get_tab_id(p)
					open_pages.append(p)
			except Exception:
				continue
		return open_pages

	async def new_background_page(self, context: BrowserContext) -> Page:
		"""Create a tab via CDP in the background so Chromium never activates its window."""
		cdp = None
		if self._browser is not None:
			try:
				existing = {id(p) for p in context.pages}
				cdp = await asyncio.wait_for(
					self._browser.new_browser_cdp_session(), timeout=CDP_OP_TIMEOUT_S
				)
				created = await asyncio.wait_for(
					cdp.send("Target.createTarget", {"url": "about:blank", "background": True}),
					timeout=CDP_OP_TIMEOUT_S,
				)
				target_id = created.get("targetId") if isinstance(created, dict) else None
				deadline = time.monotonic() + NEW_PAGE_TIMEOUT_S
				while time.monotonic() < deadline:
					new_pages = [p for p in context.pages if id(p) not in existing]
					for page in new_pages:
						if not target_id:
							return page
						page_target = await asyncio.wait_for(
							_cdp_target_id(page), timeout=CDP_OP_TIMEOUT_S
						)
						if page_target == target_id:
							return page
					await asyncio.sleep(0.05)
			except Exception:
				pass
			finally:
				if cdp is not None:
					try:
						await asyncio.wait_for(cdp.detach(), timeout=CDP_OP_TIMEOUT_S)
					except Exception:
						pass
		return await asyncio.wait_for(context.new_page(), timeout=NEW_PAGE_TIMEOUT_S)

	async def get_active_page(self, client_key: str = "default") -> Page:
		self._touch_activity()
		active = self.get_active_page_for(client_key)
		if (
			active is not None
			and (self._browser is None or self._browser.is_connected())
		):
			return active

		try:
			context = await asyncio.wait_for(self.get_context(), timeout=GET_CONTEXT_TIMEOUT_S)
			pages = await self.get_pages()
			blank_pages = [p for p in pages if p.url in ("about:blank", "chrome://newtab/", "")]
			if blank_pages:
				page = blank_pages[0]
			elif pages:
				page = pages[-1]
			else:
				page = await self.new_background_page(context)

			await _apply_animations_to_page(page)
			self.set_active_page(page, client_key=client_key)
			return page
		except Exception as exc:
			if "closed" in str(exc).lower():
				await self.close()
				context = await asyncio.wait_for(self.get_context(), timeout=GET_CONTEXT_TIMEOUT_S)
				pages = await self.get_pages()
				blank_pages = [p for p in pages if p.url in ("about:blank", "chrome://newtab/", "")]
				if blank_pages:
					page = blank_pages[0]
				elif pages:
					page = pages[-1]
				else:
					page = await self.new_background_page(context)
				await _apply_animations_to_page(page)
				self.set_active_page(page, client_key=client_key)
				return page
			raise

	async def _match_tab_name(self, name: str) -> Optional[Page]:
		if self._playwright is None:
			return None
		matched = None
		for candidate in await self.get_pages():
			try:
				title = await asyncio.wait_for(candidate.title(), timeout=PAGE_TITLE_TIMEOUT_S)
			except Exception:
				continue
			if _tab_name_from_title(title) == name or name in title.lower():
				if matched is not None:
					return None
				matched = candidate
		return matched

	async def resolve_page(self, tab: Union[int, str] = -1, client_key: str = "default") -> Page:
		self._touch_activity()
		await self.sync_tab_ids()
		if isinstance(tab, str):
			tab_str = tab.strip()
			if tab_str.lower().startswith("t"):
				tab_key = tab_str.lower()
				self._prune_closed_tabs()
				page = self._id_to_page.get(tab_key)
				if page is not None:
					try:
						is_closed = page.is_closed()
					except Exception:
						is_closed = True
					if is_closed:
						self._unregister_page(page)
						page = None
				if page is not None and self._browser is not None and not self._browser.is_connected():
					self._unregister_page(page)
					page = None
				if page is not None and self._context is not None:
					ctx_pages = getattr(self._context, "pages", None)
					if isinstance(ctx_pages, (list, tuple)) and page not in ctx_pages:
						self._unregister_page(page)
						page = None
				if page is None:
					named = await self._match_tab_name(tab_key)
					if named is not None:
						return named
					hint = self._format_open_tabs_hint()
					raise ValueError(f"Tab '{tab}' not found or was closed.{hint}")
				return page
			named = await self._match_tab_name(tab_str.lower())
			if named is not None:
				return named
			try:
				tab = int(tab_str)
			except ValueError as exc:
				raise ValueError(f"Invalid tab identifier '{tab}'. Expected an integer index, browser-derived tab ID, or tab-name substring (see status).") from exc

		if tab < 0:
			active = self.get_active_page_for(client_key)
			if (
				active is not None
				and (self._browser is None or self._browser.is_connected())
			):
				_attach_page_trackers(active)
				self.get_tab_id(active)
				return active
			return await self.get_active_page(client_key=client_key)

		pages = await self.get_pages()
		if tab >= len(pages):
			hint = self._format_open_tabs_hint(pages)
			raise ValueError(f"Invalid tab index {tab}. Total open tabs: {len(pages)}.{hint}")
		return pages[tab]


session = BrowserSessionManager(CDP_URL)


async def _apply_animations_to_page(page: Page) -> None:
	"""Apply the live reduced-motion emulation and freeze stylesheet to one page when a preference is set."""
	enabled = session.animations
	if enabled is None or page is None:
		return
	try:
		if page.is_closed():
			return
	except Exception:
		return
	motion = "no-preference" if enabled else "reduce"
	if getattr(page, "_animations_motion", None) != motion:
		try:
			await asyncio.wait_for(page.emulate_media(reduced_motion=motion), timeout=CDP_OP_TIMEOUT_S)
			setattr(page, "_animations_motion", motion)
		except Exception:
			pass
	if enabled:
		setattr(page, "_animations_css_injected", False)
	elif not getattr(page, "_animations_css_injected", False):
		try:
			await asyncio.wait_for(page.evaluate(_ANIMATIONS_INJECT_JS, _ANIMATIONS_KILL_CSS), timeout=CDP_OP_TIMEOUT_S)
			setattr(page, "_animations_css_injected", True)
		except Exception:
			pass
	if not getattr(page, "_animations_nav_hook", False):
		setattr(page, "_animations_nav_hook", True)

		def _on_dcl(_=None, p=page):
			setattr(p, "_animations_css_injected", False)
			_schedule_animations_on_page(p)

		page.on("domcontentloaded", _on_dcl)


def _schedule_animations_on_page(page: Page) -> None:
	"""Schedule the per-page animation hook from sync page-listener code paths."""
	enabled = session.animations
	if enabled is None or page is None:
		return
	motion = "no-preference" if enabled else "reduce"
	if (
		getattr(page, "_animations_motion", None) == motion
		and getattr(page, "_animations_nav_hook", False)
		and (enabled or getattr(page, "_animations_css_injected", False))
	):
		return
	try:
		asyncio.get_running_loop().create_task(_apply_animations_to_page(page))
	except RuntimeError:
		pass


class KeepAliveManager:
	"""Manages background jittered keep-alive workers to prevent web session timeouts."""

	def __init__(self):
		self._tasks: dict[Page, asyncio.Task] = {}
		self._configs: dict[Page, dict] = {}
		self._owners: dict[Page, str] = {}

	def has_active(self) -> bool:
		self._cleanup_dead_tasks()
		return bool(self._tasks)

	def _cleanup_dead_tasks(self):
		dead = [p for p, t in self._tasks.items() if p.is_closed() or t.done()]
		for p in dead:
			self._tasks.pop(p, None)
			self._configs.pop(p, None)
			self._owners.pop(p, None)

	def active_pages_with_owners(self) -> list[tuple[Page, str]]:
		self._cleanup_dead_tasks()
		return [(p, self._owners.get(p, "default")) for p in list(self._tasks)]

	def is_active(self, page: Page) -> bool:
		task = self._tasks.get(page)
		return task is not None and not task.done() and not page.is_closed()

	def get_config(self, page: Page) -> Optional[dict]:
		return self._configs.get(page)

	async def stop(self, page: Page):
		task = self._tasks.pop(page, None)
		self._configs.pop(page, None)
		self._owners.pop(page, None)
		if task is not None and not task.done():
			task.cancel()
			try:
				await task
			except asyncio.CancelledError:
				pass

	async def stop_all(self):
		tasks = list(self._tasks.values())
		self._tasks.clear()
		self._configs.clear()
		self._owners.clear()
		for t in tasks:
			if not t.done():
				t.cancel()
		if tasks:
			await asyncio.gather(*tasks, return_exceptions=True)

	async def start(
		self,
		page: Page,
		min_seconds: int = 45,
		max_seconds: int = 90,
		ping_url: Optional[str] = None,
		click_selector: Optional[str] = None,
		owner: str = "default",
	):
		await self.stop(page)
		self._owners[page] = owner
		self._configs[page] = {
			"min_seconds": min_seconds,
			"max_seconds": max_seconds,
			"ping_url": ping_url,
			"click_selector": click_selector,
			"started_at": datetime.datetime.now().isoformat(),
			"last_ping": None,
		}

		async def _worker():
			try:
				while not page.is_closed():
					delay = random.uniform(float(min_seconds), float(max_seconds))
					await asyncio.sleep(delay)
					if page.is_closed():
						break

					session._touch_activity()

					keep_working_js = """() => {
						const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
						const keepWorking = buttons.find(b => {
							const t = (b.innerText || '').toLowerCase();
							return t.includes('keep working') || t.includes('continue session') || t.includes('stay signed in') || t.includes('extend session');
						});
						if (!keepWorking) return null;
						const r = keepWorking.getBoundingClientRect();
						return { x: r.x + r.width / 2, y: r.y + r.height / 2, text: (keepWorking.innerText || '').slice(0, 60) };
					}"""
					ping_result = {"modal_clicked": None, "selector_clicked": False, "error": None}
					try:
						if click_selector:
							loc = page.locator(click_selector).first
							if await loc.count() > 0 and await loc.is_visible():
								await loc.click(timeout=5000)
								ping_result["selector_clicked"] = True
					except Exception as e:
						ping_result["error"] = f"click_selector: {e}"
					try:
						viewport = page.viewport_size or {"width": 1280, "height": 720}
						x = random.randint(100, max(101, viewport["width"] - 100))
						y = random.randint(100, max(101, viewport["height"] - 100))
						await page.mouse.move(x, y, steps=5)
						await page.mouse.move(x + random.randint(-40, 40), y + random.randint(-40, 40), steps=5)
						await page.mouse.wheel(0, random.choice([60, -60, 0]))
						target = await page.evaluate(keep_working_js)
						if target:
							await page.mouse.click(target["x"], target["y"])
							ping_result["modal_clicked"] = target["text"]
						if ping_url:
							await page.evaluate("async (u) => { try { return (await fetch(u)).status } catch (e) { return e.message } }", ping_url)
					except Exception as e:
						ping_result["error"] = f"input: {e}"
					if page in self._configs:
						self._configs[page]["last_ping"] = {
							"timestamp": datetime.datetime.now().isoformat(),
							"details": ping_result,
						}
			except asyncio.CancelledError:
				pass
			finally:
				self._tasks.pop(page, None)
				self._configs.pop(page, None)

		task = asyncio.create_task(_worker())
		self._tasks[page] = task


keep_alive_mgr = KeepAliveManager()

async def set_page_media_state(page: Page, block_media: bool):
	"""Enable or disable media blocking on a given page."""
	setattr(page, "_media_blocked", block_media)
	await network_blocker.set_media_blocked(page, block_media)


@mcp.tool()
@_with_tool_deadline
async def status(ctx: Optional[Context] = None) -> str:
	"""Check connection status to Stealth Browser and list all open tabs."""
	try:
		client_key = _session_key(ctx)
		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		await session.sync_tab_ids(pages)
		active_page = session.get_active_page_for(client_key)
		mode_str = "Headless" if session.headless else "Headed"
		lines = [f"Connected to browser session at {CDP_URL} (Mode: {mode_str})"]
		lines.append(f"Total open tabs: {len(pages)}")
		if session.animations is not None:
			anim_state = "FROZEN" if session.animations is False else "ENABLED"
			lines.append(f"Animations: {anim_state}")
		async def _safe_title(p: Page) -> str:
			try:
				return await asyncio.wait_for(p.title(), timeout=2.0)
			except Exception:
				return "(Unresponsive / Busy)"

		titles = await asyncio.gather(*(_safe_title(p) for p in pages)) if pages else []
		for idx, (page, title) in enumerate(zip(pages, titles)):
			tab_id = session.get_tab_id(page)
			active_marker = " (ACTIVE in this session)" if page == active_page else ""
			media_status = " [TEXT-ONLY/MEDIA BLOCKED]" if getattr(page, "_media_blocked", False) else ""
			animations_status = " [ANIMATIONS FROZEN]" if getattr(page, "_animations_motion", None) == "reduce" else ""
			keep_alive_status = " [KEEP-ALIVE ACTIVE]" if keep_alive_mgr.is_active(page) else ""
			lines.append(f"\t[{idx} | {tab_id} | {_tab_name_from_title(title)}] {title} - {page.url}{media_status}{animations_status}{keep_alive_status}{active_marker}")
		dialog_lines = []
		unhandled = getattr(session, "_unhandled_task_exceptions", None)
		if unhandled is not None and unhandled.get("n"):
			lines.append(f"Unhandled task exceptions since start: {unhandled['n']}")
		for p, tab_id in list(session._page_to_id.items()):
			history = getattr(p, "_dialog_history", None)
			if not history:
				continue
			for entry in list(history)[-3:]:
				dialog_lines.append(f"\t[{tab_id}] {entry['type']}: {entry['message'][:120]} @ {entry['timestamp']}")
		if dialog_lines:
			lines.append(f"Recent dialogs (last {len(dialog_lines)}, auto-dismissed unless beforeunload):")
			lines.extend(dialog_lines[-5:])
		return "\n".join(lines)
	except Exception as exc:
		return f"Disconnected: {exc}"


@mcp.tool()
@_with_tool_deadline
async def keep_alive(
	enable: bool = True,
	tab: Union[int, str] = -1,
	min_seconds: int = 45,
	max_seconds: int = 90,
	ping_url: Optional[str] = None,
	click_selector: Optional[str] = None,
	ctx: Optional[Context] = None,
) -> str:
	"""Keep a tab session alive: trusted CDP mouse/wheel input, modal auto-click, optional click_selector and heartbeat ping_url."""
	try:
		client_key = _session_key(ctx)
		page = await _resolve_page(tab, ctx)
		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		target_index = pages.index(page) if page in pages else tab

		if not enable:
			await keep_alive_mgr.stop(page)
			return f"Disabled keep-alive on tab [{target_index}] ({page.url})"

		await keep_alive_mgr.start(
			page=page,
			min_seconds=min_seconds,
			max_seconds=max_seconds,
			ping_url=ping_url,
			click_selector=click_selector,
			owner=client_key,
		)
		ping_info = f", pinging '{ping_url}'" if ping_url else ""
		click_info = f", real-clicking '{click_selector}'" if click_selector else ""
		return (
			f"Activated keep-alive on tab [{target_index}] ({page.url}): "
			f"jitter interval {min_seconds}-{max_seconds}s, trusted mouse/wheel input, auto-dismiss modals enabled{click_info}{ping_info}."
		)
	except Exception as exc:
		return f"Error managing keep-alive: {exc}"


def _has_explicit_scheme(url: str) -> bool:
	if url.lower().startswith(("http://", "https://", "about:")):
		return True
	try:
		scheme = urlparse(url).scheme
	except ValueError:
		return False
	if not scheme:
		return False
	remainder = url[len(scheme) + 1:]
	# urlparse reads bare "host:port" as a scheme (dots/digits are valid scheme chars).
	# A real scheme is followed by "//" or non-numeric content, unlike a port number.
	if remainder.startswith("//"):
		return True
	return not remainder[:1].isdigit()


@mcp.tool()
@_with_tool_deadline
async def navigate(
	url: str,
	block_media: Optional[bool] = None,
	new_tab: bool = False,
	tab: Union[int, str] = -1,
	timeout_ms: int = 30000,
	wait_until: str = "domcontentloaded",
	ctx: Optional[Context] = None,
) -> str:
	"""Navigate a tab to url. block_media=True blocks images/fonts/video (None preserves state); wait_until: 'domcontentloaded'|'commit'|'load'|'networkidle'. Clamped to 60s tool cap."""
	t0 = time.perf_counter()
	try:
		client_key = _session_key(ctx)
		context = await asyncio.wait_for(session.get_context(), timeout=GET_CONTEXT_TIMEOUT_S)
		if new_tab:
			page = await session.new_background_page(context)
			await _apply_animations_to_page(page)
			session.set_active_page(page, client_key=client_key)
		else:
			page = await _resolve_page(tab, ctx)

		if block_media is not None:
			await set_page_media_state(page, block_media=block_media)
		else:
			await network_blocker.ensure_page(page)

		if not _has_explicit_scheme(url):
			url = "https://" + url

		valid_waits = {"commit", "domcontentloaded", "load", "networkidle"}
		wait_mode = wait_until if wait_until in valid_waits else "domcontentloaded"

		goto_outer_timeout = max(1.0, (timeout_ms / 1000.0)) + GOTO_OUTER_BUFFER_S
		response = None
		sw_fallback = False
		try:
			response = await asyncio.wait_for(
				page.goto(url, timeout=timeout_ms, wait_until=wait_mode),
				timeout=goto_outer_timeout,
			)
		except Exception as exc:
			if "service worker requests do not have an associated frame" in str(exc).lower():
				sw_fallback = True
				try:
					await asyncio.wait_for(
						page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5000)),
						timeout=min(timeout_ms / 1000.0, 5.0) + 1.0,
					)
				except Exception:
					pass
			else:
				raise

		if sw_fallback:
			status = "loaded (service worker)"
		else:
			status = response.status if response else "loaded"
		try:
			title = await asyncio.wait_for(page.title(), timeout=PAGE_TITLE_TIMEOUT_S)
		except Exception:
			title = "(Unavailable)"
		try:
			pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
			target_idx = pages.index(page) if page in pages else -1
		except Exception:
			target_idx = -1
		elapsed = int((time.perf_counter() - t0) * 1000)
		is_media_blocked = getattr(page, "_media_blocked", False)
		media_tag = " (media blocked)" if is_media_blocked else ""
		return f"Navigated tab [{target_idx}] to '{url}' [HTTP {status}]{media_tag} ({elapsed}ms, wait_until={wait_mode}) - Title: '{title}'"
	except (asyncio.TimeoutError, TimeoutError):
		return f"Navigation error: Timed out after {timeout_ms}ms navigating to '{url}'"
	except Exception as exc:
		if "timeout" in exc.__class__.__name__.lower():
			return f"Navigation error: Timed out after {timeout_ms}ms navigating to '{url}'"
		return f"Navigation error: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def new_tab(
	url: str = "about:blank",
	block_media: Optional[bool] = None,
	ctx: Optional[Context] = None,
) -> str:
	"""Open a new browser tab and navigate to url (default 'about:blank'). Set block_media=True for high-speed text-only browsing."""
	client_key = _session_key(ctx)
	result = await navigate(url=url, block_media=block_media, new_tab=True, ctx=ctx)
	if result.startswith("Navigation error:"):
		return result
	page = session.get_active_page_for(client_key)
	if page is None or page.is_closed():
		page = None
		match = re.match(r"Navigated tab \[(\d+)\]", result)
		if match:
			try:
				pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
				idx = int(match.group(1))
				if 0 <= idx < len(pages):
					page = pages[idx]
			except Exception:
				page = None
	if page is not None and not page.is_closed():
		await _cdp_target_id(page)
		tab_id = session.get_tab_id(page)
		try:
			title = await asyncio.wait_for(page.title(), timeout=PAGE_TITLE_TIMEOUT_S)
		except Exception:
			title = ""
		if f"tab_id={tab_id}" not in result:
			return f'{result} (tab_id={tab_id}) [name="{_tab_name_from_title(title)}"]'
	return result


def _escape_applescript(text: str) -> str:
	return text.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _notify_human(reason: str, tab_id: str = "", tab_title: str = "") -> Optional[subprocess.Popen]:
	try:
		safe_msg = _escape_applescript(reason)
		if tab_id:
			name = _tab_name_from_title(tab_title) if tab_title else ""
			safe_msg = f"{safe_msg} on tab {tab_id}" + (f" '{_escape_applescript(name)}'" if name else "")
		script = f'display notification "{safe_msg}" with title "stealth-browser: your input needed"'
		return subprocess.Popen(
			["osascript", "-e", script],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
	except Exception:
		return None


def _url_origin(url: str) -> str:
	match = re.match(r"^(https?://[^/]+)", url or "")
	return match.group(1) if match else ""


@mcp.tool()
async def wait_for_human(
	reason: str = "Complete login / 2FA / CAPTCHA",
	url_contains: str = "",
	url_not_contains: str = "",
	selector: str = "",
	timeout_s: int = 180,
	poll_interval_s: float = 1.5,
	notify: bool = True,
	tab: Union[int, str] = -1,
	ctx: Optional[Context] = None,
) -> str:
	"""Hand off a tab to the human for login/2FA/CAPTCHA without stealing focus: sends an optional macOS notification naming the tab and polls until url_contains, url_not_contains, selector visible, or URL change (switches to headed mode only if the browser currently runs headless)."""
	t0 = time.monotonic()
	notify_proc: Optional[subprocess.Popen] = None
	try:
		client_key = _session_key(ctx)
		page = await _resolve_page(tab, ctx)
		initial_url = page.url or ""
		tab_id = session.get_tab_id(page)
		switched_to_headed = False

		async with session._lifecycle_lock:
			try:
				detected_headless = await asyncio.wait_for(
					asyncio.to_thread(session._detect_cdp_headless),
					timeout=CDP_OP_TIMEOUT_S,
				)
				if detected_headless is not None:
					session.headless = detected_headless
			except Exception:
				pass

			if session.headless:
				prior_tabs: list[tuple[str, str, str]] = []
				try:
					for p in await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S):
						p_url = getattr(p, "url", "") or ""
						prior_tabs.append((session.get_tab_id(p), p_url, _url_origin(p_url)))
				except Exception:
					prior_tabs = [(tab_id, initial_url, _url_origin(initial_url))]
				token = _in_lifecycle_op.set(True)
				try:
					restart_res = await (
						restart_browser(headless=False, ctx=ctx)
						if ctx is not None
						else restart_browser(headless=False)
					)
				finally:
					_in_lifecycle_op.reset(token)
				if isinstance(restart_res, str) and restart_res.startswith("Error"):
					return f"Error switching to headed mode for human handoff: {restart_res}"
				switched_to_headed = True
				pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
				used_pages: set[Page] = set()
				for old_tid, old_url, old_origin in prior_tabs:
					matched_page = None
					if old_url and old_url != "about:blank" and not old_url.startswith("chrome://"):
						for candidate in pages:
							if candidate not in used_pages and (getattr(candidate, "url", "") or "") == old_url:
								matched_page = candidate
								break
						if matched_page is None and old_origin:
							for candidate in pages:
								if candidate not in used_pages and (getattr(candidate, "url", "") or "").startswith(old_origin):
									matched_page = candidate
									break
					if matched_page is not None:
						used_pages.add(matched_page)
						session.reassign_tab_id(matched_page, old_tid)
				resolved_page = None
				if initial_url and initial_url != "about:blank" and not initial_url.startswith("chrome://"):
					for candidate in pages:
						if candidate.url == initial_url:
							resolved_page = candidate
							break
					if resolved_page is None:
						origin = _url_origin(initial_url)
						if origin:
							for candidate in pages:
								if (candidate.url or "").startswith(origin):
									resolved_page = candidate
									break
				page = resolved_page if resolved_page is not None else await session.get_active_page(client_key=client_key)
				session.reassign_tab_id(page, tab_id)
				session.set_active_page(page, client_key=client_key)
				tab_id = session.get_tab_id(page)
			else:
				session.set_active_page(page, client_key=client_key)

		if notify:
			try:
				notify_title = await asyncio.wait_for(page.title(), timeout=2.0)
			except Exception:
				notify_title = ""
			notify_proc = _notify_human(reason, tab_id=tab_id, tab_title=notify_title)

		has_explicit_condition = bool(url_contains or url_not_contains or selector)
		interval = max(0.005, float(poll_interval_s))
		deadline = t0 + max(0.0, float(timeout_s))
		mode_note = " (switched to headed mode)" if switched_to_headed else ""

		while True:
			session._touch_activity()
			if page.is_closed():
				elapsed = time.monotonic() - t0
				return f"Human handoff ended: tab {tab_id} was closed after {elapsed:.1f}s{mode_note} (reason: {reason})"

			current_url = page.url or ""
			matched_condition = ""
			if url_contains and url_contains in current_url:
				matched_condition = f"url_contains='{url_contains}'"
			elif url_not_contains and url_not_contains not in current_url:
				matched_condition = f"url_not_contains='{url_not_contains}'"
			elif selector:
				try:
					visible = await asyncio.wait_for(page.locator(selector).first.is_visible(), timeout=2.0)
				except Exception:
					visible = False
				if visible:
					matched_condition = f"selector='{selector}' visible"
			elif not has_explicit_condition and current_url != initial_url:
				matched_condition = f"URL changed from '{initial_url}'"

			if matched_condition:
				elapsed = time.monotonic() - t0
				try:
					title = await asyncio.wait_for(page.title(), timeout=2.0)
				except Exception:
					title = "(Unavailable)"
				return (
					f"Human handoff completed ({matched_condition}) in {elapsed:.1f}s{mode_note} "
					f"[tab_id={tab_id}] - Title: '{title}' - URL: {current_url}"
				)

			remaining = deadline - time.monotonic()
			if remaining <= 0:
				elapsed = time.monotonic() - t0
				try:
					title = await asyncio.wait_for(page.title(), timeout=2.0)
				except Exception:
					title = "(Unavailable)"
				return (
					f"Timed out after {elapsed:.1f}s waiting for human ({reason}){mode_note} "
					f"[tab_id={tab_id}] - Title: '{title}' - URL: {current_url}"
				)

			await asyncio.sleep(min(interval, remaining))
	except Exception as exc:
		return f"Error during human handoff: {exc}"
	finally:
		if notify_proc is not None:
			try:
				notify_proc.poll()
			except Exception:
				pass


@mcp.tool()
@_with_tool_deadline
async def set_media(enable_media: bool = True, tab: Union[int, str] = -1, ctx: Optional[Context] = None) -> str:
	"""Toggle media loading (images, videos, web fonts) on a tab. enable_media=False blocks heavy media for fast text-only browsing."""
	try:
		page = await _resolve_page(tab, ctx)
		await set_page_media_state(page, block_media=not enable_media)
		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		target_idx = pages.index(page) if page in pages else -1
		state_str = "ENABLED (images, charts, and media will load)" if enable_media else "BLOCKED (high-speed text-only mode)"
		return f"Media loading is now {state_str} on tab [{target_idx}] ({page.url})"
	except Exception as exc:
		return f"Error updating media state: {exc}"


@mcp.tool()
@_with_tool_deadline
async def set_animations(
	enabled: bool = False,
	persist: bool = True,
	tab: Union[int, str] = -1,
	ctx: Optional[Context] = None,
) -> str:
	"""Freeze CSS animations/transitions via reduced-motion emulation + a kill-stylesheet; sites may serve reduced-motion content variants, and re-enabling fully applies only after restart_browser() because init scripts cannot be removed."""
	try:
		page = await _resolve_page(tab, ctx)
		session.animations = bool(enabled)
		if persist:
			_save_persisted_animations(bool(enabled))
		context = await asyncio.wait_for(session.get_context(), timeout=GET_CONTEXT_TIMEOUT_S)
		if not enabled:
			await session._ensure_animation_init_script(context)
		await _apply_animations_to_page(page)
		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		for other in pages:
			if other is not page:
				await _apply_animations_to_page(other)
		state = "ENABLED (reduced-motion reverted on live pages)" if enabled else "DISABLED (animations frozen)"
		return (
			f"Animations {state}. Dynamic emulation applied to {len(pages)} open tab(s); "
			"structural init script applies to future tabs. Re-enabling fully applies after restart_browser()."
		)
	except Exception as exc:
		return f"Error updating animation state: {exc}"


_BATCH_EVAL_MAX_CHARS = 2000
_BATCH_TOTAL_MAX_CHARS = 8000
_DEFAULT_OUTPUT_MAX_CHARS = 8000


def _compact_json(obj: Any) -> str:
	return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _truncate_text(text: str, max_length: int) -> str:
	if max_length > 0 and len(text) > max_length:
		return text[:max_length] + f"\n... [Truncated: {len(text)} total characters]"
	return text


def _truncate_url(url: str, max_len: int = 160) -> str:
	s = str(url or "")
	if len(s) <= max_len:
		return s
	return s[: max_len - 3] + "..."


def _truncate_lines(header_fn, lines: list[str], max_length: int, hint: str = "") -> str:
	if max_length <= 0:
		return "\n".join([header_fn(len(lines))] + lines)
	sample_header = header_fn(len(lines))
	cur = len(sample_header)
	kept: list[str] = []
	for line in lines:
		add = len(line) + 1
		if kept and cur + add + 90 > max_length:
			break
		kept.append(line)
		cur += add
	out = [header_fn(len(kept))] + kept
	if len(kept) < len(lines):
		extra = f"; {hint}" if hint else ""
		out.append(f"... [Truncated at {max_length} chars{extra}]")
	return "\n".join(out)


def _compact_html_tag(tag_str: str) -> str:
	m = re.match(r"<([a-zA-Z0-9_-]+)([^>]*)>", tag_str)
	if not m:
		return tag_str[:80]
	tag, attrs = m.group(1).lower(), m.group(2)
	id_m = re.search(r'\bid=["\']([^"\']+)["\']', attrs)
	cls_m = re.search(r'\bclass=["\']([^"\']+)["\']', attrs)
	parts = [tag]
	if id_m:
		parts.append(f"#{id_m.group(1)[:40]}")
	if cls_m:
		first_cls = cls_m.group(1).strip().split()[0][:40]
		if first_cls:
			parts.append(f".{first_cls}")
	return f"<{''.join(parts)}>"


def _format_playwright_error(exc: Exception, max_chars: int = 300) -> str:
	raw = str(exc).strip()
	if not raw:
		return exc.__class__.__name__
	parts = re.split(r"\n\s*Call log:\s*\n?", raw, maxsplit=1)
	summary = " ".join(parts[0].strip().split())
	if len(parts) == 1:
		return summary if len(summary) <= max_chars else summary[: max_chars - 3] + "..."

	log_lines = [ln.strip().lstrip("- ").strip() for ln in parts[1].splitlines() if ln.strip()]
	reason = ""
	hint = ""
	for ln in log_lines:
		low = ln.lower()
		if "intercepts pointer events" in low:
			tag_m = re.search(r"<[^>]+>", ln)
			blocker = _compact_html_tag(tag_m.group(0)) if tag_m else "overlay"
			reason = f"{blocker} intercepts pointer events"
			hint = " (try force=True)"
			break
		if any(k in low for k in (
			"element is not visible",
			"element is not enabled",
			"element is outside of the viewport",
			"element was detached",
			"strict mode violation",
		)):
			reason = re.sub(r"<[^>]+>", lambda m: _compact_html_tag(m.group(0)), ln)[:120]
			break
	if not reason:
		for ln in reversed(log_lines):
			low = ln.lower()
			if not low.startswith(("retrying ", "waiting ")) and "× waiting" not in low:
				reason = re.sub(r"<[^>]+>", lambda m: _compact_html_tag(m.group(0)), ln)[:120]
				break

	msg = f"{summary} — {reason}{hint}" if reason else summary
	return msg if len(msg) <= max_chars else msg[: max_chars - 3] + "..."


def _action_delta_suffix(page: Page, before_url: str, before_dialogs: int) -> str:
	notes: list[str] = []
	after_url = getattr(page, "url", "") or ""
	if after_url and after_url != before_url:
		notes.append(f"navigated -> {after_url}")
	hist = getattr(page, "_dialog_history", None)
	if hist is not None and len(hist) > before_dialogs:
		last = list(hist)[-1]
		notes.append(f"dialog[{last.get('type', 'alert')}]: {str(last.get('message', ''))[:80]}")
	return f" | {'; '.join(notes)}" if notes else ""


_SB_REF_SELECTOR_RE = re.compile(r'^\[data-sb-ref=["\']([^"\']+)["\']\]$')

_SNAPSHOT_JS = r"""async ({ targetSelector, maxElements, includeShadow, includeIframes, includeHrefs, filter, viewportOnly }) => {
	const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'META', 'LINK', 'HEAD', 'TITLE', 'TEMPLATE']);
	const INTERACTIVE_SEL = 'button, input, select, textarea, a[href], summary, [role="button"], [role="link"], [role="combobox"], [role="option"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="switch"]';
	const limit = Math.max(1, Number(maxElements) || 80);
	const filterStr = String(filter || '').trim();
	let filterRe = null;
	if (filterStr) {
		try {
			filterRe = new RegExp(filterStr, 'i');
		} catch (_) {
			filterRe = null;
		}
	}
	const filterLow = filterStr.toLowerCase();

	const escIdent = (s) => (typeof CSS !== 'undefined' && CSS.escape)
		? CSS.escape(String(s))
		: String(s).replace(/([^\w-])/g, '\\$1');
	const escAttr = (s) => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');

	const isVisible = (el) => {
		try {
			const win = (el.ownerDocument && el.ownerDocument.defaultView) || window;
			const style = win.getComputedStyle(el);
			if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
			const rect = el.getBoundingClientRect();
			if (!(rect.width > 0 && rect.height > 0)) return false;
			if (viewportOnly) {
				const vw = win.innerWidth || document.documentElement.clientWidth || 0;
				const vh = win.innerHeight || document.documentElement.clientHeight || 0;
				if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= vh || rect.left >= vw) return false;
			}
			return true;
		} catch (_) {
			return false;
		}
	};

	const matchesFilter = (el) => {
		if (!filterStr) return true;
		const hay = [
			(el.innerText || el.textContent || '').trim(),
			el.getAttribute('aria-label') || '',
			el.getAttribute('placeholder') || '',
			el.name || '',
			el.id || '',
			el.getAttribute('href') || '',
			el.href || '',
		].filter(Boolean).join(' ');
		return filterRe ? filterRe.test(hay) : hay.toLowerCase().includes(filterLow);
	};

	const clearRefs = (root, visited) => {
		if (!root || visited.has(root)) return;
		visited.add(root);
		try {
			root.querySelectorAll('[data-sb-ref]').forEach((el) => el.removeAttribute('data-sb-ref'));
		} catch (_) {}
		let all = [];
		try {
			all = root.querySelectorAll ? root.querySelectorAll('*') : [];
		} catch (_) {}
		for (const el of all) {
			if (el.shadowRoot) clearRefs(el.shadowRoot, visited);
			const tn = (el.tagName || '').toUpperCase();
			if (tn === 'IFRAME' || tn === 'FRAME') {
				try {
					if (el.contentDocument) clearRefs(el.contentDocument, visited);
				} catch (_) {}
			}
		}
	};

	clearRefs(document, new Set());

	let startNode = document;
	if (targetSelector && targetSelector !== 'body') {
		try {
			startNode = document.querySelector(targetSelector) || document;
		} catch (_) {
			startNode = document;
		}
	} else if (document.body) {
		startNode = document.body;
	}

	const collected = [];
	let totalMatched = 0;
	const countCap = limit * 10;
	const seen = new Set();

	const walk = (node, ctx) => {
		if (!node || totalMatched >= countCap) return;
		if (node.nodeType === 1) {
			const tn = (node.tagName || '').toUpperCase();
			if (SKIP_TAGS.has(tn) || seen.has(node)) return;
			seen.add(node);

			try {
				if (node.matches && node.matches(INTERACTIVE_SEL) && matchesFilter(node) && isVisible(node)) {
					totalMatched++;
					if (collected.length < limit) {
						collected.push({ el: node, ctx });
					}
					if (totalMatched >= countCap) return;
				}
			} catch (_) {}

			if (includeShadow && node.shadowRoot) {
				const shadowCtx = { rootNode: node.shadowRoot, inShadow: true, inIframe: ctx.inIframe };
				for (const child of node.shadowRoot.children) {
					walk(child, shadowCtx);
					if (totalMatched >= countCap) return;
				}
			}

			if (includeIframes && (tn === 'IFRAME' || tn === 'FRAME') && isVisible(node)) {
				try {
					const doc = node.contentDocument;
					if (doc && doc.documentElement) {
						const iframeCtx = { rootNode: doc, inShadow: ctx.inShadow, inIframe: true };
						walk(doc.documentElement, iframeCtx);
						if (totalMatched >= countCap) return;
					}
				} catch (_) {}
			}

			for (const child of node.children) {
				walk(child, ctx);
				if (totalMatched >= countCap) return;
			}
		} else if (node.children) {
			for (const child of node.children) {
				walk(child, ctx);
				if (totalMatched >= countCap) return;
			}
		}
	};

	if (includeIframes) {
		const frameNeedsLoad = (f) => {
			if (!isVisible(f)) return false;
			try {
				const w = f.contentWindow;
				if (!w) return false;
				void w.location.href;
			} catch (_) {
				return false;
			}
			try {
				const d = f.contentDocument;
				if (d === null) return true;
				return d.readyState === 'loading';
			} catch (_) {
				return false;
			}
		};
		const pendingFrames = () => [...document.querySelectorAll('iframe, frame')].filter(frameNeedsLoad);
		let pending = pendingFrames();
		const frameDeadline = Date.now() + 400;
		while (pending.length && Date.now() < frameDeadline) {
			await new Promise((resolve) => setTimeout(resolve, 50));
			pending = pendingFrames();
		}
	}

	walk(startNode, { rootNode: document, inShadow: false, inIframe: false });

	const isUnique = (root, sel, target) => {
		if (!sel || !root || !root.querySelectorAll) return false;
		try {
			const matches = root.querySelectorAll(sel);
			return matches.length === 1 && matches[0] === target;
		} catch (_) {
			return false;
		}
	};

	const buildSelector = (el, tag, ref, ctx) => {
		const refSel = `[data-sb-ref="${ref}"]`;
		if (ctx.inShadow || ctx.inIframe) return refSel;

		if (el.id) {
			const idSel = `#${escIdent(el.id)}`;
			if (isUnique(document, idSel, el)) return idSel;
		}
		const testid = el.getAttribute('data-testid');
		if (testid) {
			const tidSel = `[data-testid="${escAttr(testid)}"]`;
			if (isUnique(document, tidSel, el)) return tidSel;
		}
		if (el.name) {
			const nameSel = `${tag}[name="${escAttr(el.name)}"]`;
			if (isUnique(document, nameSel, el)) return nameSel;
		}
		const ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.length <= 60) {
			const ariaSel = `${tag}[aria-label="${escAttr(ariaLabel)}"]`;
			if (isUnique(document, ariaSel, el)) return ariaSel;
		}
		if (typeof el.className === 'string' && el.className.trim()) {
			const classes = el.className.trim().split(/\s+/).filter((c) => c && !c.includes(':'));
			for (const cls of classes.slice(0, 3)) {
				const clsSel = `${tag}.${escIdent(cls)}`;
				if (isUnique(document, clsSel, el)) return clsSel;
			}
		}
		if (el.type) {
			const typeSel = `${tag}[type="${escAttr(el.type)}"]`;
			if (isUnique(document, typeSel, el)) return typeSel;
		}
		if (isUnique(document, tag, el)) return tag;
		return refSel;
	};

	const origin = (window.location && window.location.origin) || '';
	const items = collected.map(({ el, ctx }, i) => {
		const ref = `e${i}`;
		el.setAttribute('data-sb-ref', ref);
		const tag = (el.tagName || '').toLowerCase();
		const selector = buildSelector(el, tag, ref, ctx);
		const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
		const ariaRaw = (el.getAttribute('aria-label') || '').trim();
		const placeholderRaw = (el.getAttribute('placeholder') || '').trim();
		const roleRaw = (el.getAttribute('role') || '').trim();

		const attrParts = [];
		if (el.id && !selector.includes(`#${el.id}`) && !selector.includes(`#${escIdent(el.id)}`)) {
			attrParts.push(`#${el.id}`);
		}
		if (el.name && !selector.includes('[name=')) {
			attrParts.push(`name=${el.name}`);
		}
		if (el.type && !selector.includes('[type=')) {
			attrParts.push(`type=${el.type}`);
		}
		if (roleRaw) {
			attrParts.push(`role=${roleRaw}`);
		}
		if (ariaRaw && !selector.includes('[aria-label=') && ariaRaw.toLowerCase() !== text.toLowerCase()) {
			attrParts.push(`aria="${ariaRaw.slice(0, 50)}"`);
		}
		if (placeholderRaw && !text && !ariaRaw) {
			attrParts.push(`placeholder="${placeholderRaw.slice(0, 40)}"`);
		}
		if (el.value !== undefined && el.value !== '' && tag !== 'button') {
			attrParts.push(`val="${String(el.value).slice(0, 40)}"`);
		}
		if (ctx.inIframe) attrParts.push('[iframe]');
		if (ctx.inShadow) attrParts.push('[shadow]');

		let href = '';
		if (includeHrefs && tag === 'a') {
			const rawHref = (el.getAttribute('href') || '').trim();
			const lowHref = rawHref.toLowerCase();
			if (rawHref && rawHref !== '#' && !lowHref.startsWith('javascript:')) {
				href = el.href || rawHref;
				if (origin && href.startsWith(origin)) {
					href = href.slice(origin.length) || '/';
				}
				if (href.length > 80) href = href.slice(0, 77) + '...';
			}
		}

		return {
			index: i,
			ref,
			tag,
			selector,
			text,
			href,
			attrs: attrParts.join(' '),
		};
	});
	return { totalMatched, totalCapped: totalMatched >= countCap, items };
}"""

_EXTRACT_JS = r"""({ containerSelector, fields, limit, includeShadow, includeIframes }) => {
	const TEXT_MAX = 500;
	const HTML_MAX = 1500;
	const cap = (v, max) => {
		if (v === null || v === undefined) return null;
		const s = String(v);
		if (s.length > max) return s.slice(0, max) + '... [truncated]';
		return s;
	};
	const roots = [];
	const seen = new Set();
	const addRoot = (root) => {
		if (root && !seen.has(root)) {
			seen.add(root);
			roots.push(root);
		}
	};
	addRoot(document);
	if (includeIframes) {
		for (const f of document.querySelectorAll('iframe, frame')) {
			try {
				const d = f.contentDocument;
				if (d && d.documentElement) addRoot(d);
			} catch (_) {}
		}
	}
	let total = 0;
	const countIn = (root) => {
		try {
			total += root.querySelectorAll(containerSelector).length;
		} catch (_) {}
		if (!includeShadow) return;
		try {
			for (const el of root.querySelectorAll('*')) {
				if (el.shadowRoot) countIn(el.shadowRoot);
			}
		} catch (_) {}
	};
	for (const root of roots) countIn(root);
	const containers = [];
	const scan = (root) => {
		if (containers.length >= limit) return;
		try {
			for (const el of root.querySelectorAll(containerSelector)) {
				containers.push(el);
				if (containers.length >= limit) return;
			}
		} catch (_) {}
		if (!includeShadow) return;
		try {
			for (const el of root.querySelectorAll('*')) {
				if (el.shadowRoot) scan(el.shadowRoot);
				if (containers.length >= limit) return;
			}
		} catch (_) {}
	};
	if (total > 0) {
		for (const root of roots) {
			scan(root);
			if (containers.length >= limit) break;
		}
	}
	const rows = containers.map((c, i) => {
		const row = { _index: i };
		for (const [name, spec] of Object.entries(fields)) {
			let val = null;
			try {
				let el = c;
				if (spec.selector) el = c.querySelector(spec.selector);
				if (el) {
					if (spec.mode === 'attr') {
						if (spec.attr === 'value' && 'value' in el) {
							val = cap(el.value !== undefined && el.value !== null ? el.value : el.getAttribute('value'), TEXT_MAX);
						} else if (spec.attr === 'checked' && 'checked' in el) {
							val = Boolean(el.checked);
						} else {
							val = cap(el.getAttribute(spec.attr), TEXT_MAX);
						}
					} else if (spec.mode === 'html') {
						val = cap(el.outerHTML, HTML_MAX);
					} else if (spec.mode === 'inner_html') {
						val = cap(el.innerHTML, HTML_MAX);
					} else {
						val = cap((el.innerText !== undefined ? el.innerText : el.textContent) || '', TEXT_MAX);
					}
				}
			} catch (_) {
				val = null;
			}
			row[name] = val;
		}
		return row;
	});
	return { url: location.href, title: document.title, total_matched: total, returned: rows.length, rows };
}"""

_JS_FIND_REF_HELPER = r"""const findByRef = (root, refId, visited) => {
	if (!root || visited.has(root)) return null;
	visited.add(root);
	const safe = String(refId).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
	try {
		const direct = root.querySelector('[data-sb-ref="' + safe + '"]');
		if (direct) return direct;
	} catch (_) {}
	let all = [];
	try {
		all = root.querySelectorAll ? root.querySelectorAll('*') : [];
	} catch (_) {}
	for (const el of all) {
		if (el.shadowRoot) {
			const hit = findByRef(el.shadowRoot, refId, visited);
			if (hit) return hit;
		}
		const tn = (el.tagName || '').toUpperCase();
		if (tn === 'IFRAME' || tn === 'FRAME') {
			try {
				if (el.contentDocument) {
					const hit = findByRef(el.contentDocument, refId, visited);
					if (hit) return hit;
				}
			} catch (_) {}
		}
	}
	return null;
};"""

_JS_CLICK_REF = "({ refId }) => {\n" + _JS_FIND_REF_HELPER + r"""
	const target = findByRef(document, refId, new Set());
	if (!target) return false;
	if (target.scrollIntoView) target.scrollIntoView({ block: 'center', inline: 'center' });
	target.click();
	return true;
}"""

_JS_TYPE_REF = "({ refId, text, clearFirst, pressEnter }) => {\n" + _JS_FIND_REF_HELPER + r"""
	const target = findByRef(document, refId, new Set());
	if (!target) return false;
	if (target.scrollIntoView) target.scrollIntoView({ block: 'center', inline: 'center' });
	if (target.focus) target.focus();
	const nextVal = clearFirst ? String(text) : (String(target.value || '') + String(text));
	const win = (target.ownerDocument && target.ownerDocument.defaultView) || window;
	const proto = target.tagName === 'TEXTAREA'
		? win.HTMLTextAreaElement && win.HTMLTextAreaElement.prototype
		: win.HTMLInputElement && win.HTMLInputElement.prototype;
	const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
	if (desc && desc.set) {
		desc.set.call(target, nextVal);
	} else {
		target.value = nextVal;
	}
	target.dispatchEvent(new Event('input', { bubbles: true }));
	target.dispatchEvent(new Event('change', { bubbles: true }));
	if (pressEnter) {
		target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
		target.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
	}
	return true;
}"""

_JS_ANNOTATE_OVERLAY = r"""() => {
	const existing = document.getElementById('__sb_annotation_overlay__');
	if (existing) existing.remove();

	const entries = [];
	const collect = (root, ox, oy, visited) => {
		if (!root || visited.has(root)) return;
		visited.add(root);
		let refs = [];
		try {
			refs = root.querySelectorAll ? root.querySelectorAll('[data-sb-ref]') : [];
		} catch (_) {}
		for (const el of refs) {
			try {
				const rect = el.getBoundingClientRect();
				if (rect.width > 0 && rect.height > 0) {
					entries.push({
						ref: el.getAttribute('data-sb-ref') || '',
						left: rect.left + ox,
						top: rect.top + oy,
						width: rect.width,
						height: rect.height
					});
				}
			} catch (_) {}
		}
		let all = [];
		try {
			all = root.querySelectorAll ? root.querySelectorAll('*') : [];
		} catch (_) {}
		for (const el of all) {
			if (el.shadowRoot) collect(el.shadowRoot, ox, oy, visited);
			const tn = (el.tagName || '').toUpperCase();
			if (tn === 'IFRAME' || tn === 'FRAME') {
				try {
					if (el.contentDocument) {
						const ir = el.getBoundingClientRect();
						collect(el.contentDocument, ox + ir.left, oy + ir.top, visited);
					}
				} catch (_) {}
			}
		}
	};

	collect(document, window.scrollX || 0, window.scrollY || 0, new Set());
	if (entries.length === 0) return 0;

	const container = document.createElement('div');
	container.id = '__sb_annotation_overlay__';
	container.style.cssText = 'position:absolute;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483647;';

	for (const item of entries) {
		const box = document.createElement('div');
		box.style.cssText = `position:absolute;left:${item.left}px;top:${item.top}px;width:${item.width}px;height:${item.height}px;border:2px solid #e11d48;box-sizing:border-box;pointer-events:none;`;
		const label = document.createElement('span');
		label.textContent = item.ref;
		const labelTop = item.top < 16 ? '0px' : '-16px';
		label.style.cssText = `position:absolute;left:-2px;top:${labelTop};background:#e11d48;color:#ffffff;font:bold 11px/14px monospace;padding:0 4px;border-radius:2px;white-space:nowrap;pointer-events:none;`;
		box.appendChild(label);
		container.appendChild(box);
	}

	(document.body || document.documentElement).appendChild(container);
	return entries.length;
}"""

_JS_REMOVE_OVERLAY = "() => { const el = document.getElementById('__sb_annotation_overlay__'); if (el) el.remove(); }"


def _normalize_ref(ref: str) -> str:
	r = (ref or "").strip()
	if r.startswith("@"):
		r = r[1:].strip()
	return r


def _ref_to_selector(ref_id: str) -> str:
	escaped = ref_id.replace("\\", "\\\\").replace('"', '\\"')
	return f'[data-sb-ref="{escaped}"]'


def _secondary_frames(page: Page) -> list:
	frames = getattr(page, "frames", None) or []
	main_frame = getattr(page, "main_frame", None)
	return [f for f in frames if f is not main_frame]


async def _locator_has_match(loc) -> bool:
	try:
		cnt = await loc.count()
		return isinstance(cnt, int) and cnt > 0
	except Exception:
		return False


async def _first_visible_locator(loc):
	try:
		cnt = await loc.count()
	except Exception:
		return None
	if not isinstance(cnt, int) or cnt <= 0:
		return None
	if cnt == 1:
		first = loc.first
		try:
			vis = await first.is_visible()
			if isinstance(vis, bool) and not vis:
				return None
		except Exception:
			pass
		return first
	items = [loc.nth(idx) for idx in range(min(cnt, 10))]

	async def _is_vis(item) -> bool:
		try:
			vis = await item.is_visible()
			return not isinstance(vis, bool) or vis
		except Exception:
			return True

	flags = await asyncio.gather(*(_is_vis(item) for item in items))
	for item, ok in zip(items, flags):
		if ok:
			return item
	return None


async def _find_text_in_scopes(scopes: list, text: str):
	coros = []
	for scope in scopes:
		if hasattr(scope, "get_by_role"):
			for role in ("button", "link", "tab", "menuitem", "option"):
				try:
					coros.append(_first_visible_locator(scope.get_by_role(role, name=text)))
				except Exception:
					continue
		if hasattr(scope, "get_by_text"):
			try:
				coros.append(_first_visible_locator(scope.get_by_text(text)))
			except Exception:
				continue
	if not coros:
		return None
	results = await asyncio.gather(*coros, return_exceptions=True)
	for cand in results:
		if cand is not None and not isinstance(cand, BaseException):
			return cand
	return None


async def _resolve_locator(page: Page, selector: str = "", ref: str = "", text: str = "") -> tuple:
	ref_id = _normalize_ref(ref)
	if not ref_id and selector:
		m = _SB_REF_SELECTOR_RE.match(selector.strip())
		if m:
			ref_id = m.group(1)

	if ref_id:
		ref_sel = _ref_to_selector(ref_id)
		label = f"ref '{ref_id}'" if ref else f"'{selector}'"
		main_loc = page.locator(ref_sel)
		secondary = _secondary_frames(page)
		if not secondary:
			return main_loc.first, label, ref_id, None
		if await _locator_has_match(main_loc):
			return main_loc.first, label, ref_id, True
		for frame in secondary:
			try:
				floc = frame.locator(ref_sel)
				if await _locator_has_match(floc):
					return floc.first, label, ref_id, True
			except Exception:
				continue
		return main_loc.first, label, ref_id, False

	if selector:
		return page.locator(selector).first, f"'{selector}'", "", True

	if text:
		label = f"text '{text}'"
		cand = await _find_text_in_scopes([page], text)
		if cand is not None:
			return cand, label, "", True
		secondary = _secondary_frames(page)
		if secondary:
			cand = await _find_text_in_scopes(secondary, text)
			if cand is not None:
				return cand, label, "", True
		return page.get_by_text(text).first, label, "", False

	return None, "", "", False


async def _js_click_ref(page: Page, ref_id: str) -> bool:
	try:
		return bool(await asyncio.wait_for(page.evaluate(_JS_CLICK_REF, {"refId": ref_id}), timeout=5.0))
	except Exception:
		return False


async def _js_type_ref(page: Page, ref_id: str, text: str, clear_first: bool = True, press_enter: bool = False) -> bool:
	try:
		return bool(
			await asyncio.wait_for(
				page.evaluate(
					_JS_TYPE_REF,
					{"refId": ref_id, "text": text, "clearFirst": clear_first, "pressEnter": press_enter},
				),
				timeout=5.0,
			)
		)
	except Exception:
		return False


def _format_snapshot_row(el: dict, fallback_idx: int, include_hrefs: bool = False) -> str:
	ref = el.get("ref") or f"e{el.get('index', fallback_idx)}"
	tag = el.get("tag", "")
	sel = str(el.get("selector", "")).strip()
	text = " ".join(str(el.get("text", "")).split())
	attrs_raw = " ".join(str(el.get("attrs", "")).split())
	href = " ".join(str(el.get("href", "") or "").split()) if include_hrefs else ""

	show_sel = bool(sel) and sel != tag and not _SB_REF_SELECTOR_RE.match(sel)
	if show_sel and attrs_raw:
		dedup_tokens = [tok for tok in attrs_raw.split(" ") if tok and tok != sel]
		attrs_raw = " ".join(dedup_tokens)

	if href:
		if len(href) > 80:
			href = href[:77] + "..."
		href_part = f"href={href}"
		attrs_raw = f"{attrs_raw} {href_part}".strip() if attrs_raw else href_part

	parts = [f"[{ref}]", f"<{tag}>"]
	if show_sel:
		parts.append(sel)
	if text:
		parts.append(f'"{text}"')
	if attrs_raw:
		parts.append(f"({attrs_raw})")
	return " ".join(parts)


_UNSET = object()
_NAV_RETRY_MARKERS = (
	"execution context was destroyed",
	"cannot find context with specified id",
	"execution context creation",
	"most likely because of a navigation",
)
_OVERLAY_INTERCEPT_MARKER = "intercepts pointer events"
_CLICK_OVERLAY_PHASE1_MS = 2000


def _is_nav_context_error(exc: Exception) -> bool:
	msg = str(exc).lower()
	return any(marker in msg for marker in _NAV_RETRY_MARKERS)


async def _wait_after_nav_error(page: Page, remaining_s: float) -> None:
	if hasattr(page, "wait_for_load_state") and remaining_s > 0.5:
		wait_ms = int(min(2000, max(200, (remaining_s - 0.3) * 1000)))
		try:
			await asyncio.wait_for(
				page.wait_for_load_state("domcontentloaded", timeout=wait_ms),
				timeout=(wait_ms / 1000.0) + 0.5,
			)
		except Exception:
			pass
	await asyncio.sleep(min(0.25, max(0.05, remaining_s * 0.25)))


async def _evaluate_with_nav_retry(
	page: Page,
	expr: str,
	arg: Any = _UNSET,
	*,
	timeout: float = 10.0,
	isolated_context: Optional[bool] = None,
	retries: int = 1,
) -> Any:
	deadline = time.monotonic() + timeout
	kwargs: dict[str, Any] = {}
	if isolated_context is not None:
		kwargs["isolated_context"] = isolated_context
	for attempt in range(retries + 1):
		remaining = max(0.5, deadline - time.monotonic())
		try:
			coro = page.evaluate(expr, **kwargs) if arg is _UNSET else page.evaluate(expr, arg, **kwargs)
			return await asyncio.wait_for(coro, timeout=remaining)
		except Exception as exc:
			rem = deadline - time.monotonic()
			if attempt < retries and _is_nav_context_error(exc) and rem > 0.5:
				await _wait_after_nav_error(page, rem)
				continue
			raise


def _is_overlay_interception_error(exc: Exception) -> bool:
	return _OVERLAY_INTERCEPT_MARKER in str(exc).lower()


async def _js_click_locator(page: Page, locator: Any, ref_id: Optional[str], timeout_ms: int) -> bool:
	js_timeout_ms = min(timeout_ms, _CLICK_OVERLAY_PHASE1_MS)
	try:
		await asyncio.wait_for(
			locator.first.evaluate("el => el.click()", timeout=js_timeout_ms),
			timeout=max(1.0, js_timeout_ms / 1000.0) + 1.0,
		)
		return True
	except Exception:
		if ref_id:
			return await _js_click_ref(page, ref_id)
		return False


async def _click_locator_with_fallback(
	page: Page,
	locator: Any,
	*,
	ref_id: Optional[str],
	matched: Optional[bool],
	force: bool,
	timeout_ms: int,
) -> bool:
	if force:
		try:
			try:
				await asyncio.wait_for(
					locator.first.evaluate("el => el.click()", timeout=timeout_ms),
					timeout=max(1.0, timeout_ms / 1000.0) + 1.0,
				)
			except Exception:
				await locator.click(timeout=timeout_ms, force=True)
		except Exception:
			if ref_id and await _js_click_ref(page, ref_id):
				return False
			raise
		return False

	first_timeout = _CLICK_OVERLAY_PHASE1_MS if timeout_ms > _CLICK_OVERLAY_PHASE1_MS else timeout_ms
	remaining_timeout = timeout_ms - first_timeout
	try:
		await locator.click(timeout=first_timeout)
		return False
	except Exception as exc:
		if _is_overlay_interception_error(exc):
			if await _js_click_locator(page, locator, ref_id, timeout_ms):
				return True
			raise
		if remaining_timeout > 0:
			try:
				await locator.click(timeout=remaining_timeout)
				return False
			except Exception as exc2:
				if _is_overlay_interception_error(exc2):
					if await _js_click_locator(page, locator, ref_id, timeout_ms):
						return True
					raise
				exc = exc2
		if ref_id:
			if matched is None:
				matched = await _locator_has_match(locator)
			if not matched and await _js_click_ref(page, ref_id):
				return False
		raise exc


@mcp.tool()
@_with_tool_deadline
async def snapshot(
	selector: str = "body",
	max_elements: int = 80,
	tab: Union[int, str] = -1,
	include_shadow: bool = True,
	include_iframes: bool = True,
	include_hrefs: bool = False,
	filter: str = "",
	viewport_only: bool = False,
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	ctx: Optional[Context] = None,
) -> str:
	"""List visible interactive elements with refs (e0, e1, ...) across DOM, Shadow DOM, and same-origin iframes. Use filter/viewport_only to narrow; include_hrefs=True to show link URLs."""
	t0 = time.perf_counter()
	try:
		page = await _resolve_page(tab, ctx)
		try:
			raw = await _evaluate_with_nav_retry(
				page,
				_SNAPSHOT_JS,
				{
					"targetSelector": selector,
					"maxElements": max_elements,
					"includeShadow": include_shadow,
					"includeIframes": include_iframes,
					"includeHrefs": include_hrefs,
					"filter": filter,
					"viewportOnly": viewport_only,
				},
				timeout=10.0,
			)
		except asyncio.TimeoutError:
			return "Error capturing snapshot: Timed out after 10000ms"
		elapsed_ms = int((time.perf_counter() - t0) * 1000)

		if isinstance(raw, dict):
			elements = raw.get("items") or []
			total_matched = int(raw.get("totalMatched", len(elements)))
			total_capped = bool(raw.get("totalCapped", False))
		else:
			elements = raw or []
			total_matched = len(elements)
			total_capped = False

		url_str = _truncate_url(page.url, 160)
		if not elements:
			filter_note = f" matching '{filter}'" if filter else ""
			return f"No interactive elements found{filter_note} on {url_str} ({elapsed_ms}ms)"

		elements = elements[:max_elements]
		rows = [_format_snapshot_row(el, idx, include_hrefs=include_hrefs) for idx, el in enumerate(elements)]
		total_label = f"{total_matched}+" if total_capped else str(total_matched)
		footer = "... [narrow with selector=, filter=, or viewport_only=True]"

		if max_length > 0:
			kept: list[str] = []
			header_reserve = len(f"Snapshot {url_str} ({elapsed_ms}ms, showing {len(rows)} of {total_label} items):") + len(footer) + 2
			budget = max(120, max_length - header_reserve)
			cur = 0
			for row in rows:
				if kept and cur + len(row) + 1 > budget:
					break
				kept.append(row)
				cur += len(row) + 1
			rows = kept

		shown = len(rows)
		is_partial = shown < total_matched or total_capped
		count_str = f"showing {shown} of {total_label} items" if is_partial else f"{shown} items"
		header = f"Snapshot {url_str} ({elapsed_ms}ms, {count_str}):"
		lines = [header] + rows
		if is_partial:
			lines.append(footer)
		return "\n".join(lines)
	except Exception as exc:
		return f"Error capturing snapshot: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def batch(
	actions: list[dict],
	tab: Union[int, str] = -1,
	ctx: Optional[Context] = None,
) -> str:
	"""Run sequential actions in one turn. Valid actions: click, fill, type, select, press, wait_for, sleep, evaluate (fields: selector, ref, text, value, key, state, ms, force, expression, main_world, timeout_ms)."""
	t0 = time.perf_counter()
	try:
		page = await _resolve_page(tab, ctx)
		before_url = getattr(page, "url", "") or ""
		before_dialogs = len(getattr(page, "_dialog_history", None) or ())
		results = []
		current_step = 0
		for idx, item in enumerate(actions):
			current_step = idx
			act = item.get("action", "").lower()
			timeout = item.get("timeout_ms", 4000)
			sel = (item.get("selector") or "").strip()
			ref = _normalize_ref(item.get("ref") or "")
			force = bool(item.get("force", False))

			if act == "click":
				text_target = item.get("text") or ""
				if not (sel or ref or text_target):
					results.append(f"[{idx}] click: SKIPPED (missing 'selector', 'ref', or 'text')")
					continue
				loc, label, ref_id, matched = await _resolve_locator(page, selector=sel, ref=ref, text=text_target)
				overlay_fallback = await _click_locator_with_fallback(
					page,
					loc,
					ref_id=ref_id,
					matched=matched,
					force=force,
					timeout_ms=timeout,
				)
				force_tag = " (force)" if force else (" (js-dispatched after overlay interception)" if overlay_fallback else "")
				results.append(f"[{idx}] click {label}{force_tag}: OK")

			elif act == "fill":
				if not (sel or ref):
					results.append(f"[{idx}] fill: SKIPPED (missing 'selector' or 'ref')")
					continue
				text_val = str(item.get("text") if item.get("text") is not None else item.get("value", ""))
				loc, label, ref_id, matched = await _resolve_locator(page, selector=sel, ref=ref)
				try:
					await loc.fill(text_val, timeout=timeout, force=force)
				except Exception:
					if ref_id:
						if not force and matched is None:
							matched = await _locator_has_match(loc)
						if (not matched or force) and await _js_type_ref(page, ref_id, text_val, clear_first=True, press_enter=False):
							pass
						else:
							raise
					else:
						raise
				results.append(f"[{idx}] fill {label}: OK")

			elif act == "type":
				if not (sel or ref):
					results.append(f"[{idx}] type: SKIPPED (missing 'selector' or 'ref')")
					continue
				text_val = str(item.get("text") if item.get("text") is not None else item.get("value", ""))
				press_enter = bool(item.get("press_enter", False))
				loc, label, ref_id, matched = await _resolve_locator(page, selector=sel, ref=ref)
				try:
					if force:
						await loc.click(timeout=timeout, force=True)
					await loc.type(text_val, timeout=timeout)
					if press_enter:
						await loc.press("Enter")
				except Exception:
					if ref_id:
						if not force and matched is None:
							matched = await _locator_has_match(loc)
						if (not matched or force) and await _js_type_ref(page, ref_id, text_val, clear_first=False, press_enter=press_enter):
							pass
						else:
							raise
					else:
						raise
				results.append(f"[{idx}] type {label} ({'with Enter' if press_enter else ''}): OK")

			elif act == "select":
				if not (sel or ref):
					results.append(f"[{idx}] select: SKIPPED (missing 'selector' or 'ref')")
					continue
				val = str(item.get("value") if item.get("value") is not None else item.get("text", ""))
				loc, label, _, _ = await _resolve_locator(page, selector=sel, ref=ref)
				await loc.select_option(val, timeout=timeout, force=force)
				results.append(f"[{idx}] select {label} option '{val}': OK")

			elif act == "press":
				key = item.get("key") or item.get("button", "Enter")
				await page.keyboard.press(key)
				results.append(f"[{idx}] press '{key}': OK")

			elif act == "wait_for":
				if not (sel or ref):
					results.append(f"[{idx}] wait_for: SKIPPED (missing 'selector' or 'ref')")
					continue
				state = item.get("state", "visible")
				loc, label, _, _ = await _resolve_locator(page, selector=sel, ref=ref)
				await loc.wait_for(state=state, timeout=timeout)
				results.append(f"[{idx}] wait_for {label} ({state}): OK")

			elif act == "sleep":
				ms = item.get("ms") or item.get("duration_ms", 200)
				await asyncio.sleep(ms / 1000.0)
				results.append(f"[{idx}] sleep {ms}ms: OK")

			elif act == "evaluate":
				expr = item.get("expression") or item.get("code") or item.get("js", "")
				main_world = bool(item.get("main_world", False))
				res = await asyncio.wait_for(
					page.evaluate(expr, isolated_context=not main_world),
					timeout=timeout / 1000.0,
				)
				res_str = _compact_json(res) if isinstance(res, (dict, list)) else str(res)
				res_str = _truncate_text(res_str, _BATCH_EVAL_MAX_CHARS)
				results.append(f"[{idx}] evaluate: {res_str}")

			else:
				results.append(f"[{idx}] Unknown action '{act}': SKIPPED")

		elapsed = int((time.perf_counter() - t0) * 1000)
		delta = _action_delta_suffix(page, before_url, before_dialogs)
		out = f"Batch completed ({len(results)} steps in {elapsed}ms) on {page.url}{delta}:\n" + "\n".join(results)
		return _truncate_text(out, _BATCH_TOTAL_MAX_CHARS)
	except Exception as exc:
		elapsed = int((time.perf_counter() - t0) * 1000)
		completed = len(results) if "results" in locals() else 0
		step_idx = current_step if "current_step" in locals() else 0
		total = len(actions)
		msg = f"Batch failed at step [{step_idx}] ({completed}/{total} completed) after {elapsed}ms: {_format_playwright_error(exc)}"
		if completed:
			msg = f"{msg}\nCompleted:\n" + "\n".join(results)
		return _truncate_text(msg, _BATCH_TOTAL_MAX_CHARS)


_GET_CONTENT_CLEAN_JS = r"""({ sel }) => {
	const el = document.querySelector(sel);
	if (!el) return null;
	const JUNK_SEL = 'script, style, svg, noscript, nav, header, footer, [role="banner"], [role="navigation"], [aria-hidden="true"], .cookie-banner, .cookie-consent, #cookie-banner, .advertisement, .ad-banner';
	const BLOCK_DISPLAY = new Set(['block', 'flex', 'grid', 'list-item', 'table', 'table-row', 'table-caption', 'flow-root']);
	const parts = [];
	const walk = (node) => {
		if (!node) return;
		if (node.nodeType === 3) {
			const t = (node.nodeValue || '').replace(/[\t\r\n ]+/g, ' ');
			if (t.trim()) {
				parts.push(t);
			} else if (t && parts.length && !parts[parts.length - 1].endsWith('\n') && !parts[parts.length - 1].endsWith(' ')) {
				parts.push(' ');
			}
			return;
		}
		if (node.nodeType !== 1) return;
		let cs = null;
		if (node !== el) {
			if (node.hidden) return;
			try { if (node.matches && node.matches(JUNK_SEL)) return; } catch (_) {}
			try {
				cs = window.getComputedStyle(node);
				if (!cs || cs.display === 'none' || cs.visibility === 'hidden') return;
			} catch (_) {}
		}
		const tag = (node.tagName || '').toUpperCase();
		if (tag === 'BR') {
			parts.push('\n');
			return;
		}
		if (cs === null) {
			try { cs = window.getComputedStyle(node); } catch (_) {}
		}
		const isBlock = Boolean(cs && BLOCK_DISPLAY.has(cs.display));
		if (isBlock && parts.length && !parts[parts.length - 1].endsWith('\n')) parts.push('\n');
		for (const child of node.childNodes) walk(child);
		if (isBlock && parts.length && !parts[parts.length - 1].endsWith('\n')) parts.push('\n');
	};
	walk(el);
	return parts.join('')
		.split('\n')
		.map((l) => l.replace(/[ \t]+/g, ' ').trim())
		.join('\n')
		.replace(/\n{3,}/g, '\n\n')
		.trim();
}"""


def _filter_content_lines(text: str, pattern: str) -> Optional[str]:
	lines = text.splitlines()
	try:
		rx = re.compile(pattern, re.IGNORECASE)
	except re.error:
		rx = re.compile(re.escape(pattern), re.IGNORECASE)
	matched_indices = [i for i, ln in enumerate(lines) if rx.search(ln)]
	if not matched_indices:
		return None
	keep: set[int] = set()
	for idx in matched_indices:
		for j in range(max(0, idx - 1), min(len(lines), idx + 2)):
			keep.add(j)
	out: list[str] = []
	prev = -2
	for idx in sorted(keep):
		if prev >= 0 and idx > prev + 1:
			out.append("--")
		out.append(lines[idx])
		prev = idx
	return "\n".join(out)


@mcp.tool()
@_with_tool_deadline
async def get_content(
	selector: str = "body",
	max_length: int = 4000,
	interactive_only: bool = False,
	clean: bool = True,
	tab: Union[int, str] = -1,
	offset: int = 0,
	filter: str = "",
	ctx: Optional[Context] = None,
) -> str:
	"""Extract visible text from a tab. Supports offset (negative for tail, e.g. -3000), filter (regex/substring with context), clean=True (strips nav/hidden/whitespace), or interactive_only=True (calls snapshot)."""
	try:
		if interactive_only:
			return await snapshot(selector=selector, tab=tab, filter=filter, ctx=ctx)

		page = await _resolve_page(tab, ctx)
		is_body = selector.strip().lower() == "body"
		if clean:
			raw_text = await _evaluate_with_nav_retry(page, _GET_CONTENT_CLEAN_JS, {"sel": selector}, timeout=10.0)
			if raw_text is None and is_body:
				await _wait_after_nav_error(page, 2.5)
				raw_text = await _evaluate_with_nav_retry(page, _GET_CONTENT_CLEAN_JS, {"sel": selector}, timeout=8.0)
			if raw_text is None:
				return f"Element '{selector}' not found on page {page.url}"
			text = str(raw_text)
			if "  " in text or "\t" in text or "\n\n\n" in text:
				text = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
				text = re.sub(r"\n{3,}", "\n\n", text).strip()
		else:
			text = None
			for attempt in range(2):
				try:
					element = page.locator(selector).first
					if await element.count() == 0:
						if attempt == 0 and is_body:
							await _wait_after_nav_error(page, 2.5)
							continue
						return f"Element '{selector}' not found on page {page.url}"
					text = str(await asyncio.wait_for(element.inner_text(), timeout=10.0) or "").strip()
					break
				except Exception as exc:
					if attempt == 0 and _is_nav_context_error(exc):
						await _wait_after_nav_error(page, 2.5)
						continue
					raise

		if not text:
			return "(Page element contains no visible text)"

		filter_clean = (filter or "").strip()
		if filter_clean:
			filtered = _filter_content_lines(text, filter_clean)
			if filtered is None:
				return f"No lines matched filter '{filter_clean}' in '{selector}' on {page.url} ({len(text)} total chars)"
			text = filtered

		total_len = len(text)
		if offset > 0 and offset >= total_len:
			return f"Offset {offset} is past end of content ({total_len} total chars)."
		start = max(0, total_len + offset) if offset < 0 else offset
		end = min(total_len, start + max_length) if max_length > 0 else total_len
		if start > 0 or end < total_len:
			chunk = text[start:end]
			tail_note = f"\n... (truncated; pass offset={end} for next chunk)" if end < total_len else ""
			return f"[Showing chars {start}-{end} of {total_len}]:\n{chunk}{tail_note}"
		return text
	except Exception as exc:
		return f"Error extracting content: {_format_playwright_error(exc)}"


_EXTRACT_MAX_FIELDS = 12
_EXTRACT_MAX_LIMIT = 200


def _normalize_extract_fields(fields) -> Optional[dict[str, dict[str, str]]]:
	if not isinstance(fields, dict) or not fields:
		return None
	if len(fields) > _EXTRACT_MAX_FIELDS:
		return None
	normalized: dict[str, dict[str, str]] = {}
	for name, spec in fields.items():
		key = str(name).strip()
		if not key or len(key) > 64:
			return None
		mode, selector, attr = "text", "", ""
		if isinstance(spec, dict):
			mode = str(spec.get("mode") or "text").strip().lower()
			selector = str(spec.get("selector") or "").strip()
			attr = str(spec.get("attr") or "").strip()
		elif isinstance(spec, str) and spec.strip():
			raw = spec.strip()
			head, sep, tail = raw.partition(" ")
			head_l = head.strip().lower()
			if sep and head_l in ("text", "html", "inner_html"):
				mode, selector = head_l, tail.strip()
			elif sep and head_l.startswith("@") and len(head_l) > 1:
				mode, attr, selector = "attr", head_l[1:], tail.strip()
			else:
				m = re.match(r"^(text|html|inner_html):(.+)$", raw)
				if m:
					mode, selector = m.group(1), m.group(2).strip()
				elif re.match(r"^@([\w.-]+)$", raw):
					mode, attr = "attr", raw[1:]
				elif raw in ("text", "html", "inner_html"):
					mode = raw
				else:
					mode, selector = "text", raw
		else:
			return None
		if mode not in ("text", "attr", "html", "inner_html"):
			return None
		if mode == "attr" and not attr:
			return None
		normalized[key] = {"mode": mode, "selector": selector, "attr": attr}
	return normalized


@mcp.tool()
@_with_tool_deadline
async def extract(
	container_selector: str,
	fields: dict[str, Union[str, dict]],
	limit: int = 20,
	include_shadow: bool = True,
	include_iframes: bool = True,
	tab: Union[int, str] = -1,
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	ctx: Optional[Context] = None,
) -> str:
	"""Extract JSON rows from containers (cards/tables/forms) across DOM/Shadow/iframes. fields: name -> 'text:.sel'|'@attr .sel' ('@value'/'@checked' for inputs)|'html:.sel'|{selector,mode,attr}."""
	t0 = time.perf_counter()
	try:
		normalized = _normalize_extract_fields(fields)
		if normalized is None:
			return (
				"Error extracting rows: 'fields' must be a non-empty map of at most "
				f"{_EXTRACT_MAX_FIELDS} field names to a spec string (e.g. '.title', 'text:.title', '@href a.link', "
				"'@value #price', 'html:.thumb') or {'selector': ..., 'mode': 'text'|'attr'|'html'|'inner_html', 'attr': ...}."
			)
		limit = min(max(1, int(limit)), _EXTRACT_MAX_LIMIT)
		container_selector = (container_selector or "").strip()
		if not container_selector:
			return "Error extracting rows: 'container_selector' must be non-empty."
		page = await _resolve_page(tab, ctx)
		result = await _evaluate_with_nav_retry(
			page,
			_EXTRACT_JS,
			{
				"containerSelector": container_selector,
				"fields": normalized,
				"limit": limit,
				"includeShadow": include_shadow,
				"includeIframes": include_iframes,
			},
			timeout=15.0,
		)
		elapsed_ms = int((time.perf_counter() - t0) * 1000)
		if not isinstance(result, dict) or "rows" not in result:
			return f"Error extracting rows: unexpected result from page ({result.__class__.__name__})."
		if not result["rows"]:
			return (
				f"No elements matched '{container_selector}' on {page.url} "
				f"({elapsed_ms}ms, total_matched={result.get('total_matched', 0)})."
			)
		formatted = _truncate_text(_compact_json(result), max_length)
		return f"Extracted {result['returned']} row(s) from '{container_selector}' ({elapsed_ms}ms):\n{formatted}"
	except asyncio.TimeoutError:
		return "Error extracting rows: Timed out after 15000ms"
	except Exception as exc:
		return f"Error extracting rows: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def click(
	selector: str = "",
	ref: str = "",
	text: str = "",
	force: bool = False,
	tab: Union[int, str] = -1,
	timeout_ms: int = 5000,
	ctx: Optional[Context] = None,
) -> str:
	"""Click an element by CSS selector, snapshot ref ('e12' or '@e12'), or visible text. Set force=True to bypass pointer-interception overlays."""
	sel_clean = (selector or "").strip()
	ref_clean = _normalize_ref(ref)
	if not (sel_clean or ref_clean or text):
		return "Error clicking element: provide at least one of 'selector', 'ref', or 'text'"
	label = f"ref '{ref_clean}'" if ref_clean else (f"'{sel_clean}'" if sel_clean else f"text '{text}'")
	try:
		page = await _resolve_page(tab, ctx)
		before_url = getattr(page, "url", "") or ""
		before_dialogs = len(getattr(page, "_dialog_history", None) or ())
		locator, label, ref_id, matched = await _resolve_locator(page, selector=sel_clean, ref=ref_clean, text=text)
		overlay_fallback = await _click_locator_with_fallback(
			page,
			locator,
			ref_id=ref_id,
			matched=matched,
			force=force,
			timeout_ms=timeout_ms,
		)
		force_tag = " (force)" if force else (" (js-dispatched after overlay interception)" if overlay_fallback else "")
		delta = _action_delta_suffix(page, before_url, before_dialogs)
		return f"Clicked element matching {label}{force_tag} on {page.url}{delta}"
	except Exception as exc:
		return f"Error clicking element {label}: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def type(
	selector: str = "",
	text: str = "",
	clear_first: bool = True,
	press_enter: bool = False,
	tab: Union[int, str] = -1,
	timeout_ms: int = 5000,
	ref: str = "",
	ctx: Optional[Context] = None,
) -> str:
	"""Type text into an input/textarea by CSS selector or snapshot ref ('e3' or '@e3')."""
	sel_clean = (selector or "").strip()
	ref_clean = _normalize_ref(ref)
	if not (sel_clean or ref_clean):
		return "Error typing: provide either 'selector' or 'ref'"
	label = f"ref '{ref_clean}'" if ref_clean else f"'{sel_clean}'"
	try:
		page = await _resolve_page(tab, ctx)
		before_url = getattr(page, "url", "") or ""
		before_dialogs = len(getattr(page, "_dialog_history", None) or ())
		locator, label, ref_id, matched = await _resolve_locator(page, selector=sel_clean, ref=ref_clean)
		try:
			if clear_first:
				await locator.fill(text, timeout=timeout_ms)
			else:
				await locator.type(text, timeout=timeout_ms)
			if press_enter:
				await locator.press("Enter")
		except Exception:
			if ref_id:
				if matched is None:
					matched = await _locator_has_match(locator)
				if not matched and await _js_type_ref(page, ref_id, text, clear_first=clear_first, press_enter=press_enter):
					pass
				else:
					raise
			else:
				raise

		delta = _action_delta_suffix(page, before_url, before_dialogs)
		if press_enter:
			return f"Typed text into {label} and pressed Enter{delta}"
		return f"Typed text into {label}{delta}"
	except Exception as exc:
		return f"Error typing into {label}: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def press_key(key: str, tab: Union[int, str] = -1, ctx: Optional[Context] = None) -> str:
	"""Press a keyboard key (e.g., 'Enter', 'Tab', 'Escape')."""
	try:
		page = await _resolve_page(tab, ctx)
		before_url = getattr(page, "url", "") or ""
		before_dialogs = len(getattr(page, "_dialog_history", None) or ())
		await page.keyboard.press(key)
		delta = _action_delta_suffix(page, before_url, before_dialogs)
		return f"Pressed key '{key}' on {page.url}{delta}"
	except Exception as exc:
		return f"Error pressing key '{key}': {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def scroll(
	direction: str = "down",
	amount: int = 600,
	tab: Union[int, str] = -1,
	ctx: Optional[Context] = None,
) -> str:
	"""Scroll up or down by amount pixels."""
	try:
		page = await _resolve_page(tab, ctx)
		delta = amount if direction.lower() == "down" else -amount
		await page.mouse.wheel(0, delta)
		return f"Scrolled {direction} by {amount}px"
	except Exception as exc:
		return f"Error scrolling: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def screenshot(
	save_path: str = "",
	tab: Union[int, str] = -1,
	full_page: bool = False,
	selector: str = "",
	ref: str = "",
	annotate: bool = False,
	ctx: Optional[Context] = None,
) -> str:
	"""Capture a screenshot of the active page, full page, or a specific element (by selector or snapshot ref). Set annotate=True to temporarily overlay snapshot ref labels (e0, e1, ...)."""
	try:
		page = await _resolve_page(tab, ctx)
		if not save_path:
			timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
			save_path = f"/tmp/screenshot_{timestamp}.png"

		sel_clean = (selector or "").strip()
		ref_clean = _normalize_ref(ref)
		annotated_count = 0
		try:
			if annotate:
				annotated_count = int(await asyncio.wait_for(page.evaluate(_JS_ANNOTATE_OVERLAY), timeout=10.0) or 0)
				if annotated_count == 0:
					await (snapshot(tab=tab, ctx=ctx) if ctx is not None else snapshot(tab=tab))
					annotated_count = int(await asyncio.wait_for(page.evaluate(_JS_ANNOTATE_OVERLAY), timeout=10.0) or 0)

			if sel_clean or ref_clean:
				locator, _, _, _ = await _resolve_locator(page, selector=sel_clean, ref=ref_clean)
				await locator.screenshot(path=save_path)
			else:
				await page.screenshot(path=save_path, full_page=full_page)
		finally:
			if annotate:
				try:
					await asyncio.wait_for(page.evaluate(_JS_REMOVE_OVERLAY), timeout=5.0)
				except Exception:
					pass

		title = await page.title()
		extras = []
		if sel_clean or ref_clean:
			extras.append(f"Element: {f'ref {ref_clean}' if ref_clean else sel_clean}")
		elif full_page:
			extras.append("Mode: full_page")
		if annotate:
			extras.append(f"Annotated: {annotated_count} ref(s)")
		extra_line = ("\n" + " | ".join(extras)) if extras else ""
		return f"Screenshot saved to: {save_path}\nPage Title: {title}\nURL: {page.url}{extra_line}"
	except Exception as exc:
		return f"Error taking screenshot: {_format_playwright_error(exc)}"


@mcp.tool()
@_with_tool_deadline
async def fetch(
	url: str,
	method: str = "GET",
	headers: Optional[dict] = None,
	body: Optional[Any] = None,
	response_type: str = "auto",
	tab: Union[int, str] = -1,
	timeout_ms: int = 15000,
	save_path: str = "",
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	ctx: Optional[Context] = None,
) -> str:
	"""Execute an authenticated in-browser fetch (supports relative, absolute, and blob: URLs). Pass save_path to write binary/PDF responses to disk without entering context."""
	t0 = time.perf_counter()
	try:
		page = await _resolve_page(tab, ctx)

		if isinstance(body, (dict, list)):
			body = json.dumps(body)
			if headers is None:
				headers = {}
			headers.setdefault("Content-Type", "application/json")
		save_binary = bool(save_path and save_path.strip())
		js_script = """async ({ url, method, headers, body, responseType, saveBinary }) => {
			const options = {
				method: (method || 'GET').toUpperCase(),
				credentials: 'include',
				headers: headers || {}
			};
			if (body && !['GET', 'HEAD'].includes(options.method)) {
				options.body = body;
			}
			const tStart = performance.now();
			try {
				const res = await window.fetch(url, options);
				const duration = Math.round(performance.now() - tStart);
				const resHeaders = {};
				res.headers.forEach((v, k) => { resHeaders[k] = v; });

				if (saveBinary) {
					if (!res.ok) {
						let errorPreview = '';
						try {
							errorPreview = (await res.text()).slice(0, 1000);
						} catch (_) {}
						return {
							ok: false,
							status: res.status,
							statusText: res.statusText,
							url: res.url,
							duration,
							headers: resHeaders,
							errorPreview
						};
					}
					const buf = await res.arrayBuffer();
					const bytes = new Uint8Array(buf);
					// Multiple of 3 so each btoa chunk avoids intermediate padding and call-stack overflow
					const chunkSize = 12288;
					const b64Parts = [];
					for (let i = 0; i < bytes.length; i += chunkSize) {
						const slice = bytes.subarray(i, Math.min(i + chunkSize, bytes.length));
						b64Parts.push(btoa(String.fromCharCode.apply(null, slice)));
					}
					return {
						ok: true,
						status: res.status,
						statusText: res.statusText,
						url: res.url,
						duration,
						headers: resHeaders,
						base64: b64Parts.join(''),
						sizeBytes: bytes.byteLength
					};
				}

				const text = await res.text();
				let parsed = null;
				let isJson = false;
				if (responseType === 'json' || responseType === 'auto') {
					try {
						parsed = JSON.parse(text);
						isJson = true;
					} catch (_) {}
				}
				return {
					ok: res.ok,
					status: res.status,
					statusText: res.statusText,
					url: res.url,
					duration,
					headers: resHeaders,
					isJson,
					data: isJson ? parsed : text
				};
			} catch (err) {
				return {
					error: err.toString(),
					duration: Math.round(performance.now() - tStart)
				};
			}
		}"""
		try:
			result = await asyncio.wait_for(
				page.evaluate(js_script, {
					"url": url,
					"method": method,
					"headers": headers or {},
					"body": body,
					"responseType": response_type,
					"saveBinary": save_binary,
				}),
				timeout=timeout_ms / 1000.0,
			)
		except asyncio.TimeoutError:
			return f"Error executing fetch: Timed out after {timeout_ms}ms"
		elapsed_total = int((time.perf_counter() - t0) * 1000)

		if "error" in result:
			return f"Fetch error ({result.get('duration', elapsed_total)}ms): {result['error']}"

		status_str = f"HTTP {result['status']} {result['statusText']} ({result['duration']}ms)"
		if save_binary:
			dest_path = os.path.abspath(os.path.expanduser(save_path.strip()))
			res_headers = result.get("headers") or {}
			content_type = next(
				(v for k, v in res_headers.items() if k.lower() == "content-type"),
				"application/octet-stream",
			)
			if not result.get("ok"):
				preview = (result.get("errorPreview") or "").strip()
				msg = (
					f"Fetch failed — {status_str} from {result['url']} "
					f"(Content-Type: {content_type}); file not saved to {dest_path}"
				)
				if preview:
					msg += f"\nResponse preview: {preview}"
				return msg
			raw_bytes = base64.b64decode(result.get("base64") or "")
			parent_dir = os.path.dirname(dest_path)
			if parent_dir:
				os.makedirs(parent_dir, exist_ok=True)
			with open(dest_path, "wb") as f:
				f.write(raw_bytes)
			return (
				f"{status_str} [SAVED] from {result['url']} -> {dest_path} "
				f"(Content-Type: {content_type}, {len(raw_bytes)} bytes / {_format_byte_size(len(raw_bytes))})"
			)

		data = result.get("data")
		if result.get("isJson") and data is not None:
			dumped = _truncate_text(_compact_json(data), max_length)
			return f"{status_str} [JSON] from {result['url']}:\n\n{dumped}"
		else:
			text = _truncate_text(str(data or ""), max_length)
			return f"{status_str} [TEXT] from {result['url']}:\n\n{text}"
	except Exception as exc:
		return f"Error executing fetch: {exc}"


def _format_byte_size(size_bytes: Optional[int]) -> str:
	if size_bytes is None:
		return "unknown size"
	if size_bytes < 1024:
		return f"{size_bytes} B"
	if size_bytes < 1024 * 1024:
		return f"{size_bytes / 1024:.1f} KB"
	return f"{size_bytes / (1024 * 1024):.2f} MB"


class DownloadTracker:
	"""Tracks Playwright download events in a bounded ring buffer without blocking the event loop."""

	def __init__(self, maxlen: int = 100, downloads_dir: Optional[str] = None):
		self._entries: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
		self._next_id: int = 1
		self._pending_tasks: set[asyncio.Task] = set()
		self._downloads_dir_override: Optional[str] = downloads_dir

	@property
	def downloads_dir(self) -> str:
		return self._downloads_dir_override if self._downloads_dir_override is not None else DOWNLOADS_DIR

	def attach(self, page: Page) -> None:
		if page is None or getattr(page, "_download_tracker_attached", False):
			return
		setattr(page, "_download_tracker_attached", True)
		page.on("download", self._on_download)

	def _resolve_native_path(self, suggested_filename: str, started_ts: float) -> Optional[str]:
		target_dir = self.downloads_dir
		if not suggested_filename or not os.path.isdir(target_dir):
			return None
		stem, ext = os.path.splitext(suggested_filename)
		recent_candidates: list[tuple[float, str]] = []
		try:
			with os.scandir(target_dir) as it:
				for de in it:
					if not de.is_file():
						continue
					name = de.name
					if name == suggested_filename or (name.startswith(f"{stem} (") and name.endswith(f"){ext}")):
						try:
							mtime = de.stat().st_mtime
						except OSError:
							continue
						if mtime >= started_ts - 2.0:
							recent_candidates.append((mtime, de.path))
		except OSError:
			return None
		if recent_candidates:
			recent_candidates.sort(key=lambda item: item[0], reverse=True)
			return os.path.abspath(recent_candidates[0][1])
		direct = os.path.join(target_dir, suggested_filename)
		if os.path.isfile(direct):
			return os.path.abspath(direct)
		return None

	def _on_download(self, download: Any) -> None:
		started_ts = time.time()
		try:
			url = str(getattr(download, "url", "") or "")
		except Exception:
			url = ""
		try:
			suggested_filename = str(getattr(download, "suggested_filename", "") or "")
		except Exception:
			suggested_filename = ""

		entry: dict[str, Any] = {
			"id": self._next_id,
			"url": url,
			"suggested_filename": suggested_filename,
			"save_path": None,
			"status": "in_progress",
			"size_bytes": None,
			"started_at": datetime.datetime.now().isoformat(timespec="seconds"),
			"completed_at": None,
			"error": None,
		}
		self._next_id += 1
		self._entries.append(entry)

		try:
			loop = asyncio.get_running_loop()
		except RuntimeError:
			return
		task = loop.create_task(self._await_completion(entry, download, started_ts))
		self._pending_tasks.add(task)
		task.add_done_callback(self._pending_tasks.discard)

	async def _await_completion(self, entry: dict[str, Any], download: Any, started_ts: float) -> None:
		try:
			failure = await download.failure()
		except Exception as exc:
			native_path = self._resolve_native_path(entry["suggested_filename"], started_ts)
			if native_path is not None:
				self._mark_completed(entry, native_path)
			else:
				self._mark_failed(entry, str(exc))
			return

		if failure:
			self._mark_failed(entry, str(failure))
			return

		path_err: Optional[str] = None
		save_path: Optional[str] = None
		try:
			raw_path = await download.path()
			if raw_path is not None:
				save_path = str(raw_path)
		except Exception as exc:
			path_err = str(exc)

		if not save_path or not os.path.isfile(save_path):
			native_path = self._resolve_native_path(entry["suggested_filename"], started_ts)
			if native_path is not None:
				save_path = native_path

		if save_path:
			self._mark_completed(entry, save_path)
		elif path_err:
			self._mark_failed(entry, path_err)
		else:
			self._mark_completed(entry, None)

	def _mark_completed(self, entry: dict[str, Any], save_path: Optional[str]) -> None:
		entry["status"] = "completed"
		entry["save_path"] = save_path
		if save_path and os.path.isfile(save_path):
			try:
				entry["size_bytes"] = os.path.getsize(save_path)
			except OSError:
				entry["size_bytes"] = None
		entry["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")

	def _mark_failed(self, entry: dict[str, Any], error: str) -> None:
		entry["status"] = "failed"
		entry["error"] = error
		entry["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")

	async def flush(self) -> None:
		if self._pending_tasks:
			await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

	def list_entries(self, limit: int = 20) -> list[dict[str, Any]]:
		if limit <= 0:
			return []
		return list(self._entries)[-limit:][::-1]

	def list_dir_files(self, limit: int = 20) -> list[dict[str, Any]]:
		if limit <= 0 or not os.path.isdir(self.downloads_dir):
			return []
		files: list[tuple[float, dict[str, Any]]] = []
		try:
			with os.scandir(self.downloads_dir) as it:
				for de in it:
					if de.name.startswith(".") or not de.is_file():
						continue
					try:
						st = de.stat()
					except OSError:
						continue
					files.append((
						st.st_mtime,
						{
							"name": de.name,
							"path": os.path.abspath(de.path),
							"size_bytes": st.st_size,
							"modified_at": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
						},
					))
		except OSError:
			return []
		files.sort(key=lambda item: item[0], reverse=True)
		return [item[1] for item in files[:limit]]


class TelemetryTracker:
	"""Maintains per-page bounded ring buffers for console messages and network responses/failures."""

	MAX_CONSOLE_TEXT = 500

	def __init__(self, maxlen: int = 200):
		self._maxlen = maxlen
		self._console: dict[Page, collections.deque[dict[str, Any]]] = {}
		self._network: dict[Page, collections.deque[dict[str, Any]]] = {}
		self._cdp_sessions: dict[Page, Any] = {}

	def attach(self, page: Page) -> None:
		if page is None or getattr(page, "_telemetry_tracker_attached", False):
			return
		setattr(page, "_telemetry_tracker_attached", True)
		self._console.setdefault(page, collections.deque(maxlen=self._maxlen))
		self._network.setdefault(page, collections.deque(maxlen=self._maxlen))

		page.on("console", lambda msg: self._on_console(page, msg))
		page.on("pageerror", lambda err: self._on_pageerror(page, err))
		page.on("response", lambda resp: self._on_response(page, resp))
		page.on("requestfailed", lambda req: self._on_requestfailed(page, req))
		page.on("close", lambda _=None: self._on_close(page))
		page.on("crash", lambda _=None: self._on_crash(page))

	async def ensure_console_capture(self, page: Page) -> bool:
		"""Attach a raw-CDP Runtime console listener to a tab (lazily, on first console_logs call).

		patchright never sends Runtime.enable (stealth), so page.on("console") only sees
		browser-generated Log entries. Enabling it on-demand trades a small amount of stealth
		on that single tab for full script console capture.
		"""
		if page is None or page.is_closed():
			return False
		if page in self._cdp_sessions:
			return True
		context = getattr(page, "context", None)
		if context is None:
			return False
		try:
			cdp = await asyncio.wait_for(context.new_cdp_session(page), timeout=10.0)
		except Exception:
			return False

		def _on_console_api(ev: Any) -> None:
			try:
				level = str(ev.get("type", "log") or "log").lower()
				if level == "warn":
					level = "warning"
				parts: list[str] = []
				for arg in ev.get("args", []) or []:
					val = arg.get("value")
					if val is None:
						val = arg.get("description")
					parts.append(str(val) if val is not None else "(opaque object)")
				text = self._truncate_text(" ".join(parts))
				location = ""
				stack = ev.get("stackTrace") or []
				if isinstance(stack, list) and stack and isinstance(stack[0], dict):
					location = f"{stack[0].get('url', '')}:{stack[0].get('lineNumber', '')}"
				buf = self._console.setdefault(page, collections.deque(maxlen=self._maxlen))
				buf.append({
					"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
					"level": level,
					"text": text,
					"location": location,
				})
			except Exception:
				pass

		def _on_exception_thrown(ev: Any) -> None:
			try:
				details = ev.get("exceptionDetails") or {}
				text = str(details.get("text") or "")
				exc = details.get("exception") or {}
				desc = exc.get("description")
				buf = self._console.setdefault(page, collections.deque(maxlen=self._maxlen))
				buf.append({
					"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
					"level": "pageerror",
					"text": self._truncate_text(str(desc) if desc else text),
					"location": "",
				})
			except Exception:
				pass

		cdp.on("Runtime.consoleAPICalled", _on_console_api)
		cdp.on("Runtime.exceptionThrown", _on_exception_thrown)

		def _drop_session(_=None) -> None:
			self._drop_cdp_session(page, cdp)

		page.on("close", _drop_session)
		page.on("crash", _drop_session)
		try:
			await asyncio.wait_for(cdp.send("Runtime.enable"), timeout=5.0)
		except Exception:
			await self._detach_quiet(cdp)
			return False
		self._cdp_sessions[page] = cdp
		return True

	def _drop_cdp_session(self, page: Page, cdp) -> None:
		if self._cdp_sessions.get(page) is cdp:
			self._cdp_sessions.pop(page, None)
			self._schedule_detach(cdp)

	@staticmethod
	async def _detach_quiet(cdp) -> None:
		try:
			await cdp.detach()
		except Exception:
			pass

	@staticmethod
	def _schedule_detach(cdp) -> None:
		try:
			asyncio.get_running_loop().create_task(TelemetryTracker._detach_quiet(cdp))
		except Exception:
			pass

	def _on_close(self, page: Page) -> None:
		cdp = self._cdp_sessions.pop(page, None)
		self._console.pop(page, None)
		self._network.pop(page, None)
		if cdp is not None:
			self._schedule_detach(cdp)

	def _on_crash(self, page: Page) -> None:
		self._cdp_sessions.pop(page, None)

	def _truncate_text(self, text: str) -> str:
		if len(text) > self.MAX_CONSOLE_TEXT:
			return text[:self.MAX_CONSOLE_TEXT] + "... [truncated]"
		return text

	def _on_console(self, page: Page, msg: Any) -> None:
		buf = self._console.setdefault(page, collections.deque(maxlen=self._maxlen))
		try:
			level = str(getattr(msg, "type", "log") or "log").lower()
		except Exception:
			level = "log"
		if level == "warn":
			level = "warning"
		try:
			raw_text = str(getattr(msg, "text", "") or "")
		except Exception:
			raw_text = ""
		location = ""
		try:
			loc = getattr(msg, "location", None)
			if isinstance(loc, dict) and loc.get("url"):
				line_no = loc.get("lineNumber")
				location = f"{loc['url']}:{line_no}" if line_no is not None else str(loc["url"])
		except Exception:
			location = ""
		buf.append({
			"timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
			"level": level,
			"text": self._truncate_text(raw_text),
			"location": location,
		})

	def _on_pageerror(self, page: Page, err: Any) -> None:
		buf = self._console.setdefault(page, collections.deque(maxlen=self._maxlen))
		try:
			raw_text = str(getattr(err, "message", None) or err)
		except Exception:
			raw_text = str(err)
		buf.append({
			"timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
			"level": "pageerror",
			"text": self._truncate_text(raw_text),
			"location": "",
		})

	@staticmethod
	def _resolve_headers(item: dict[str, Any]) -> None:
		raw_headers = item.pop("_raw_headers", None)
		if not isinstance(raw_headers, dict):
			return
		content_type = raw_headers.get("content-type")
		content_length = raw_headers.get("content-length")
		if content_type is None or content_length is None:
			for k, v in raw_headers.items():
				kl = k.lower()
				if kl == "content-type" and content_type is None:
					content_type = v
				elif kl == "content-length" and content_length is None:
					content_length = v
		if content_type is not None:
			item["content_type"] = str(content_type)
		if content_length is not None:
			try:
				item["size_bytes"] = int(str(content_length).strip())
			except ValueError:
				pass

	def _on_response(self, page: Page, resp: Any) -> None:
		buf = self._network.setdefault(page, collections.deque(maxlen=self._maxlen))
		req = getattr(resp, "request", None)
		method = str(getattr(req, "method", "GET") or "GET").upper()
		resource_type = str(getattr(req, "resource_type", "other") or "other").lower()
		try:
			status = int(getattr(resp, "status", 0))
		except Exception:
			status = None
		url = str(getattr(resp, "url", "") or getattr(req, "url", "") or "")

		try:
			raw_headers = getattr(resp, "headers", None)
		except Exception:
			raw_headers = None

		duration_ms: Optional[int] = None
		try:
			timing = getattr(req, "timing", None)
			if isinstance(timing, dict):
				resp_end = timing.get("responseEnd")
				resp_start = timing.get("responseStart")
				if isinstance(resp_end, (int, float)) and resp_end > 0:
					duration_ms = int(round(resp_end))
				elif isinstance(resp_start, (int, float)) and resp_start >= 0:
					duration_ms = int(round(resp_start))
		except Exception:
			pass

		entry: dict[str, Any] = {
			"timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
			"method": method,
			"status": status,
			"failure": None,
			"resource_type": resource_type,
			"content_type": "",
			"url": url,
			"size_bytes": None,
			"duration_ms": duration_ms,
		}
		if isinstance(raw_headers, dict) and raw_headers:
			entry["_raw_headers"] = raw_headers
		buf.append(entry)

	def _on_requestfailed(self, page: Page, req: Any) -> None:
		buf = self._network.setdefault(page, collections.deque(maxlen=self._maxlen))
		method = str(getattr(req, "method", "GET") or "GET").upper()
		resource_type = str(getattr(req, "resource_type", "other") or "other").lower()
		url = str(getattr(req, "url", "") or "")
		raw_failure = getattr(req, "failure", None)
		if isinstance(raw_failure, dict):
			failure = str(raw_failure.get("errorText") or raw_failure)
		elif raw_failure:
			failure = str(raw_failure)
		else:
			failure = "failed"
		buf.append({
			"timestamp": datetime.datetime.now().isoformat(timespec="milliseconds"),
			"method": method,
			"status": None,
			"failure": failure,
			"resource_type": resource_type,
			"content_type": "",
			"url": url,
			"size_bytes": None,
			"duration_ms": None,
		})

	def get_network(
		self,
		page: Page,
		url_filter: str = "",
		resource_type: str = "",
		content_type: str = "",
		status_min: int = 0,
		limit: int = 50,
		clear: bool = False,
	) -> tuple[list[dict[str, Any]], int]:
		buf = self._network.get(page)
		if not buf:
			return [], 0
		total = len(buf)
		entries = list(buf)
		if clear:
			buf.clear()

		url_sub = url_filter.strip().lower()
		allowed_types = {t.strip().lower() for t in resource_type.split(",") if t.strip()} if resource_type else set()
		ct_sub = content_type.strip().lower()

		matched = []
		for item in entries:
			if url_sub and url_sub not in item["url"].lower():
				continue
			if allowed_types and item["resource_type"].lower() not in allowed_types:
				continue
			if status_min > 0 and (item["status"] is None or item["status"] < status_min):
				continue
			self._resolve_headers(item)
			if ct_sub and ct_sub not in item["content_type"].lower():
				continue
			matched.append(item)

		if limit <= 0:
			return [], total
		return matched[-limit:], total

	def get_console(
		self,
		page: Page,
		level: str = "",
		limit: int = 50,
		clear: bool = False,
	) -> tuple[list[dict[str, Any]], int]:
		buf = self._console.get(page)
		if not buf:
			return [], 0
		total = len(buf)
		entries = list(buf)
		if clear:
			buf.clear()

		allowed_levels: set[str] = set()
		if level:
			for part in level.split(","):
				norm = part.strip().lower()
				if norm == "warn":
					norm = "warning"
				if norm:
					allowed_levels.add(norm)

		matched = [item for item in entries if not allowed_levels or item["level"].lower() in allowed_levels]
		if limit <= 0:
			return [], total
		return matched[-limit:], total


download_tracker = DownloadTracker()
telemetry_tracker = TelemetryTracker()


def _attach_page_trackers(page: Page) -> None:
	download_tracker.attach(page)
	telemetry_tracker.attach(page)
	_schedule_animations_on_page(page)


@mcp.tool()
@_with_tool_deadline
async def downloads(limit: int = 20, include_dir: bool = False) -> str:
	"""List tracked browser downloads (and recent DOWNLOADS_DIR files when include_dir=True)."""
	try:
		entries = download_tracker.list_entries(limit=limit)
		sections: list[str] = []
		if entries:
			lines = [f"Tracked browser downloads (showing {len(entries)} of {len(download_tracker._entries)}):"]
			for item in entries:
				size_str = _format_byte_size(item["size_bytes"])
				dest = item["save_path"] or "(pending)"
				url_str = _truncate_url(item["url"], 140)
				line = (
					f"- [#{item['id']}] [{item['status']}] {item['suggested_filename'] or '(unnamed)'} "
					f"({size_str}) -> {dest} | url: {url_str} | started: {item['started_at']}"
				)
				if item["completed_at"]:
					line += f" | completed: {item['completed_at']}"
				if item["error"]:
					line += f" | error: {item['error']}"
				lines.append(line)
			sections.append("\n".join(lines))
		else:
			hint = "" if include_dir else f" Pass include_dir=True to list files in {download_tracker.downloads_dir}."
			sections.append(f"No tracked browser downloads yet.{hint}")

		if include_dir:
			dir_files = download_tracker.list_dir_files(limit=limit)
			if dir_files:
				dir_lines = [f"Recent files in {download_tracker.downloads_dir} (showing {len(dir_files)}):"]
				for f in dir_files:
					dir_lines.append(
						f"- {f['name']} ({_format_byte_size(f['size_bytes'])}, modified {f['modified_at']}) -> {f['path']}"
					)
				sections.append("\n".join(dir_lines))
			else:
				sections.append(f"No files found in {download_tracker.downloads_dir}.")

		return "\n\n".join(sections)
	except Exception as exc:
		return f"Error listing downloads: {exc}"


@mcp.tool()
@_with_tool_deadline
async def network_requests(
	url_filter: str = "",
	resource_type: str = "",
	content_type: str = "",
	status_min: int = 0,
	limit: int = 30,
	clear: bool = False,
	tab: Union[int, str] = -1,
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	ctx: Optional[Context] = None,
) -> str:
	"""List captured network responses/failures for a tab. Filter by url_filter, resource_type ('xhr,fetch'), content_type ('json','pdf'), or status_min (e.g. 400)."""
	try:
		page = await _resolve_page(tab, ctx)
		_attach_page_trackers(page)
		entries, total = telemetry_tracker.get_network(
			page=page,
			url_filter=url_filter,
			resource_type=resource_type,
			content_type=content_type,
			status_min=status_min,
			limit=limit,
			clear=clear,
		)
		cleared_tag = " [buffer cleared]" if clear else ""
		if not entries:
			return f"No matching network requests for {page.url} ({total} total captured){cleared_tag}."

		lines: list[str] = []
		for item in entries:
			ts = item["timestamp"].split("T")[-1]
			if item["failure"]:
				status_part = f"FAILED({item['failure']})"
			else:
				status_part = str(item["status"])
			meta = []
			if item["content_type"]:
				meta.append(item["content_type"])
			if item["size_bytes"] is not None:
				meta.append(_format_byte_size(item["size_bytes"]))
			if item["duration_ms"] is not None:
				meta.append(f"{item['duration_ms']}ms")
			meta_str = f" ({', '.join(meta)})" if meta else ""
			lines.append(f"- [{ts}] {status_part} {item['method']} [{item['resource_type']}]{meta_str} {_truncate_url(item['url'], 160)}")
		return _truncate_lines(
			lambda shown: f"Network requests for {page.url} (showing {shown} of {total} captured){cleared_tag}:",
			lines,
			max_length,
			hint="filter with url_filter=/resource_type=",
		)
	except Exception as exc:
		return f"Error listing network requests: {exc}"


@mcp.tool()
@_with_tool_deadline
async def console_logs(
	level: str = "",
	limit: int = 50,
	clear: bool = False,
	tab: Union[int, str] = -1,
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	ctx: Optional[Context] = None,
) -> str:
	"""List captured browser console messages and page errors (filter by comma-separated level: 'error,warning,log,pageerror'). Attaches CDP console listener on first call."""
	try:
		page = await _resolve_page(tab, ctx)
		_attach_page_trackers(page)
		await telemetry_tracker.ensure_console_capture(page)
		entries, total = telemetry_tracker.get_console(
			page=page,
			level=level,
			limit=limit,
			clear=clear,
		)
		cleared_tag = " [buffer cleared]" if clear else ""
		if not entries:
			return f"No matching console logs for {page.url} ({total} total captured){cleared_tag}."

		lines: list[str] = []
		for item in entries:
			ts = item["timestamp"].split("T")[-1]
			loc_str = f" ({_truncate_url(item['location'], 120)})" if item["location"] else ""
			lines.append(f"- [{ts}] [{item['level']}]{loc_str} {item['text']}")
		return _truncate_lines(
			lambda shown: f"Console logs for {page.url} (showing {shown} of {total} captured){cleared_tag}:",
			lines,
			max_length,
			hint="filter with level=",
		)
	except Exception as exc:
		return f"Error listing console logs: {exc}"


@mcp.tool()
@_with_tool_deadline
async def state(
	framework: str = "auto",
	selector: str = "",
	tab: Union[int, str] = -1,
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	ctx: Optional[Context] = None,
) -> str:
	"""Inspect in-memory client-side state (framework: 'auto'|'next'|'redux'|'react'|'vue'|'apollo'|'query'; optional selector for React Fiber)."""
	t0 = time.perf_counter()
	try:
		page = await _resolve_page(tab, ctx)
		js_script = """({ targetFramework, selector }) => {
			function safeSerialize(obj, maxDepth = 4, maxKeys = 80) {
				const seen = new WeakSet();
				function helper(val, depth) {
					if (val === null || typeof val !== 'object') {
						if (typeof val === 'function') return '[Function]';
						if (typeof val === 'symbol') return val.toString();
						return val;
					}
					if (val instanceof Element || val instanceof Node) {
						return `<${val.tagName ? val.tagName.toLowerCase() : 'Node'}${val.id ? '#' + val.id : ''}>`;
					}
					if (seen.has(val)) return '[Circular]';
					if (depth >= maxDepth) return Array.isArray(val) ? `[Array(${val.length})]` : '[Object]';
					seen.add(val);

					if (Array.isArray(val)) {
						const res = [];
						const len = Math.min(val.length, 60);
						for (let i = 0; i < len; i++) {
							res.push(helper(val[i], depth + 1));
						}
						if (val.length > len) res.push(`... ${val.length - len} more items`);
						return res;
					}

					const res = {};
					const keys = Object.keys(val).slice(0, maxKeys);
					for (const k of keys) {
						try {
							res[k] = helper(val[k], depth + 1);
						} catch (_) {
							res[k] = '[Unserializable]';
						}
					}
					return res;
				}
				return helper(obj, 0);
			}

			const results = [];
			const fw = (targetFramework || 'auto').toLowerCase();

			// 1. Next.js Hydration Data
			if ((fw === 'auto' || fw === 'next') && window.__NEXT_DATA__) {
				results.push({
					source: 'Next.js (__NEXT_DATA__)',
					keys: Object.keys(window.__NEXT_DATA__),
					data: safeSerialize(window.__NEXT_DATA__)
				});
			}

			// 2. Nuxt / SSR Initial State
			if ((fw === 'auto' || fw === 'vue') && window.__NUXT__) {
				results.push({
					source: 'Nuxt (__NUXT__)',
					keys: Object.keys(window.__NUXT__),
					data: safeSerialize(window.__NUXT__)
				});
			}
			if ((fw === 'auto') && window.__INITIAL_STATE__) {
				results.push({
					source: 'Window Initial State (__INITIAL_STATE__)',
					keys: Object.keys(window.__INITIAL_STATE__),
					data: safeSerialize(window.__INITIAL_STATE__)
				});
			}

			// 3. Global Redux Store
			if ((fw === 'auto' || fw === 'redux') && window.store && typeof window.store.getState === 'function') {
				const state = window.store.getState();
				results.push({
					source: 'Redux (window.store)',
					keys: Object.keys(state || {}),
					data: safeSerialize(state)
				});
			}

			// 4. Apollo GraphQL Cache
			if ((fw === 'auto' || fw === 'apollo') && window.__APOLLO_CLIENT__ && typeof window.__APOLLO_CLIENT__.extract === 'function') {
				const cache = window.__APOLLO_CLIENT__.extract();
				results.push({
					source: 'Apollo GraphQL Cache',
					keys: Object.keys(cache || {}),
					data: safeSerialize(cache)
				});
			}

			// 5. TanStack / React Query Client
			if ((fw === 'auto' || fw === 'query') && window.queryClient && typeof window.queryClient.getQueryCache === 'function') {
				const queries = window.queryClient.getQueryCache().getAll().map(q => ({
					queryKey: q.queryKey,
					status: q.state?.status,
					data: q.state?.data
				}));
				results.push({
					source: 'React Query (window.queryClient)',
					keys: queries.map(q => JSON.stringify(q.queryKey)),
					data: safeSerialize(queries)
				});
			}

			// 6. Vue Component Tree
			if (fw === 'auto' || fw === 'vue') {
				const vueEl = document.querySelector(selector || '#app, [data-v-app], #root');
				if (vueEl) {
					if (vueEl.__vue_app__) {
						const appState = vueEl.__vue_app__._instance?.data || vueEl.__vue_app__._context?.provides;
						if (appState) {
							results.push({
								source: 'Vue 3 (__vue_app__)',
								keys: Object.keys(appState),
								data: safeSerialize(appState)
							});
						}
					} else if (vueEl.__vue__) {
						results.push({
							source: 'Vue 2 (__vue__)',
							keys: Object.keys(vueEl.__vue__.$data || {}),
							data: safeSerialize(vueEl.__vue__.$data)
						});
					}
				}
			}

			// 7. React Fiber Traversal (Redux Provider or Component Props/State)
			if (fw === 'auto' || fw === 'react' || fw === 'redux') {
				const targetEl = selector ? document.querySelector(selector) : (document.querySelector('#root') || document.querySelector('#app') || document.body);
				if (targetEl) {
					const fiberKey = Object.keys(targetEl).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
					if (fiberKey) {
						let current = targetEl[fiberKey];
						let foundStore = false;

						// Walk up fiber tree to check for Redux context or QueryClient context
						let ancestor = current;
						let steps = 0;
						while (ancestor && steps < 50) {
							// Redux context check
							if (!foundStore && ancestor.memoizedProps?.store?.getState) {
								const state = ancestor.memoizedProps.store.getState();
								results.push({
									source: 'Redux Store (via React Fiber Context)',
									keys: Object.keys(state || {}),
									data: safeSerialize(state)
								});
								foundStore = true;
							}
							ancestor = ancestor.return;
							steps++;
						}

						// Specific component fiber props & state
						if (selector || (!results.length && targetEl !== document.body)) {
							results.push({
								source: `React Fiber Node (${targetEl.tagName.toLowerCase()}${targetEl.id ? '#' + targetEl.id : ''})`,
								keys: current.memoizedProps ? Object.keys(current.memoizedProps) : [],
								props: safeSerialize(current.memoizedProps),
								state: safeSerialize(current.memoizedState)
							});
						}
					}
				}
			}

			return results;
		}"""
		try:
			# Patchright defaults to an isolated world where window.__NEXT_DATA__/Redux/Fiber expandos are invisible
			results = await _evaluate_with_nav_retry(
				page,
				js_script,
				{
					"targetFramework": framework,
					"selector": selector,
				},
				timeout=10.0,
				isolated_context=False,
			)
		except asyncio.TimeoutError:
			return f"Error detecting state: Timed out after 10000ms"
		elapsed_ms = int((time.perf_counter() - t0) * 1000)

		if not results:
			return (
				f"No in-memory state stores (Next.js, Redux, React Fiber, Vue, Apollo, React Query) detected "
				f"on {page.url} ({elapsed_ms}ms). The page may be server-rendered or state is privately scoped in closures."
			)

		lines = [f"In-Memory State Detective Results for {page.url} ({elapsed_ms}ms):", ""]
		for idx, item in enumerate(results):
			lines.append(f"### [{idx + 1}] Source: {item['source']}")
			if "keys" in item and item["keys"]:
				key_preview = ", ".join(f"`{k}`" for k in item["keys"][:20])
				if len(item["keys"]) > 20:
					key_preview += f" ... (+{len(item['keys']) - 20} more)"
				lines.append(f"- **Top-Level Keys ({len(item['keys'])} total)**: {key_preview}")

			payload = item["data"] if "data" in item else {"props": item.get("props"), "state": item.get("state")}
			dumped = _compact_json(payload)
			lines.append("```json")
			lines.append(dumped)
			lines.append("```\n")

		out = "\n".join(lines)
		if max_length > 0 and len(out) > max_length:
			out = out[:max_length] + f"\n... [Truncated: {len(out)} total characters; narrow with framework= or selector=]"
		return out
	except Exception as exc:
		return f"Error detecting state: {exc}"


@mcp.tool()
@_with_tool_deadline
async def evaluate_js(
	expression: str = "",
	tab: Union[int, str] = -1,
	timeout_ms: int = 15000,
	script: str = "",
	max_length: int = _DEFAULT_OUTPUT_MAX_CHARS,
	main_world: bool = False,
	ctx: Optional[Context] = None,
) -> str:
	"""Evaluate JS in isolated world by default (alias: script=). Set main_world=True for page globals."""
	code = expression if expression and expression.strip() else script
	if not code or not code.strip():
		return "JavaScript execution error: 'expression' (or alias 'script') must be non-empty."
	try:
		page = await _resolve_page(tab, ctx)
		result = await asyncio.wait_for(
			page.evaluate(code, isolated_context=not main_world),
			timeout=timeout_ms / 1000.0,
		)
		if isinstance(result, (dict, list)):
			formatted = _compact_json(result)
		else:
			formatted = str(result)
		formatted = _truncate_text(formatted, max_length)
		return f"Result: {formatted}"
	except asyncio.TimeoutError:
		return f"JavaScript execution error: Timed out after {timeout_ms}ms"
	except Exception as exc:
		return f"JavaScript execution error: {exc}"


@mcp.tool()
@_with_tool_deadline
async def wait_for(
	selector: str = "",
	text: str = "",
	url_contains: str = "",
	state: str = "visible",
	timeout_ms: int = 15000,
	tab: Union[int, str] = -1,
	ctx: Optional[Context] = None,
) -> str:
	"""Wait for a CSS selector state, visible page text substring, URL substring, or combination. Clamped to 60s cap."""
	if not selector and not text and not url_contains:
		return "Error in wait_for: specify at least one of 'selector', 'text', or 'url_contains'."
	valid_states = {"attached", "detached", "hidden", "visible"}
	if state not in valid_states:
		return f"Error in wait_for: invalid state '{state}'. Expected one of: {', '.join(sorted(valid_states))}."
	if timeout_ms <= 0:
		return f"Error in wait_for: timeout_ms must be > 0 (got {timeout_ms})."
	timeout_ms = min(timeout_ms, int((TOOL_TOTAL_TIMEOUT_S - 10.0) * 1000))

	t0 = time.perf_counter()
	deadline = time.monotonic() + (timeout_ms / 1000.0)
	pending_desc = ""
	page: Optional[Page] = None

	try:
		page = await _resolve_page(tab, ctx)
		matched: list[str] = []

		if url_contains:
			pending_desc = f"url containing '{url_contains}'"
			while url_contains not in (page.url or ""):
				if page.is_closed():
					return f"Error in wait_for: tab closed while waiting for {pending_desc}"
				if time.monotonic() >= deadline:
					raise asyncio.TimeoutError()
				await asyncio.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
			matched.append(f"url_contains='{url_contains}'")

		if selector:
			pending_desc = f"selector '{selector}' ({state})"
			rem_s = deadline - time.monotonic()
			if rem_s <= 0:
				raise asyncio.TimeoutError()
			await asyncio.wait_for(
				page.locator(selector).first.wait_for(state=state, timeout=max(1, int(rem_s * 1000))),
				timeout=rem_s + 2.0,
			)
			matched.append(f"selector='{selector}' ({state})")

		if text:
			pending_desc = f"visible text '{text}'"
			while True:
				if page.is_closed():
					return f"Error in wait_for: tab closed while waiting for {pending_desc}"
				rem_s = deadline - time.monotonic()
				if rem_s <= 0:
					raise asyncio.TimeoutError()
				slice_s = min(rem_s, 3.0)
				slice_ms = max(1, int(slice_s * 1000))
				js_wait_text = """async (needle) => {
					const hasVisibleText = () => {
						const root = document.body || document.documentElement;
						if (!root) return false;
						if (!(root.textContent || '').includes(needle)) return false;
						return (root.innerText || '').includes(needle);
					};
					if (hasVisibleText()) return true;
					const target = document.documentElement || document;
					return await new Promise((resolve) => {
						let done = false;
						let timer = null;
						const finish = (val) => {
							if (done) return;
							done = true;
							observer.disconnect();
							if (timer !== null) clearTimeout(timer);
							resolve(val);
						};
						const observer = new MutationObserver(() => {
							if (hasVisibleText()) finish(true);
						});
						observer.observe(target, {
							childList: true,
							subtree: true,
							characterData: true,
							attributes: true,
							attributeFilter: ['style', 'class', 'hidden', 'aria-hidden'],
						});
						timer = setTimeout(() => finish(hasVisibleText()), %d);
					});
				}""" % slice_ms
				try:
					found = await asyncio.wait_for(
						page.evaluate(js_wait_text, text),
						timeout=slice_s + 1.0,
					)
				except asyncio.TimeoutError:
					if time.monotonic() >= deadline:
						raise
					found = False
				except Exception:
					found = False
					if time.monotonic() < deadline:
						await asyncio.sleep(min(0.05, max(0.01, deadline - time.monotonic())))
				if found:
					break
				if time.monotonic() >= deadline:
					raise asyncio.TimeoutError()
			matched.append(f"text='{text}'")

		elapsed_ms = int((time.perf_counter() - t0) * 1000)
		return f"Wait condition met ({', '.join(matched)}) on {page.url} ({elapsed_ms}ms)"
	except (asyncio.TimeoutError, TimeoutError):
		curr_url = page.url if page is not None else "unknown"
		return f"Wait timed out after {timeout_ms}ms waiting for {pending_desc or 'condition'} on {curr_url}"
	except Exception as exc:
		if "timeout" in exc.__class__.__name__.lower():
			curr_url = page.url if page is not None else "unknown"
			return f"Wait timed out after {timeout_ms}ms waiting for {pending_desc or 'condition'} on {curr_url}"
		return f"Error in wait_for: {_format_playwright_error(exc)}"


def _check_sibling_sessions(action_desc: str, client_key: str, force: bool, force_hint: str) -> Optional[str]:
	if force or _in_lifecycle_op.get():
		return None
	siblings = session.sibling_active_tabs(client_key)
	if not siblings:
		return None
	lines = []
	for other_key, tab_id, page, ka_active in siblings:
		ka_tag = " [KEEP-ALIVE ACTIVE]" if ka_active else ""
		url = getattr(page, "url", "") or ""
		lines.append(f"\t[{tab_id}] ({other_key}) {url}{ka_tag}")
	tab_list = "\n".join(lines)
	return (
		f"Refusing to {action_desc} because {len(siblings)} sibling session(s) have active tab(s) "
		f"(this would disrupt other connected clients):\n"
		f"{tab_list}\n"
		f"Ask before interrupting sibling sessions, or call {force_hint} to proceed."
	)


@mcp.tool()
@_with_tool_deadline
async def switch_tab(
	tab: Union[int, str, None] = None,
	index: Optional[int] = None,
	ctx: Optional[Context] = None,
) -> str:
	"""Switch tab by index, browser-derived id, or name substring (see status). Accepts 'tab'/'index'."""
	target = tab if tab is not None else index
	if target is None:
		return "Error switching tab: provide a 'tab' (or 'index') value."
	try:
		client_key = _session_key(ctx)
		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		await session.sync_tab_ids(pages)
		if isinstance(target, str):
			norm = target.strip().lower()
			resolved = next((i for i, p in enumerate(pages) if session.get_tab_id(p) == norm), -1)
			if resolved < 0:
				resolved = await _resolve_tab_name(session, pages, norm)
			if resolved < 0:
				hint = session._format_open_tabs_hint(pages)
				return f"No open tab with id or name '{norm}'.{hint or ' Use status() to list open tabs.'}"
			target = resolved
		if target < 0 or target >= len(pages):
			hint = session._format_open_tabs_hint(pages)
			return f"Invalid tab index {target}. Total open tabs: {len(pages)}.{hint}"

		page = pages[target]
		session.set_active_page(page, client_key=client_key)
		try:
			title = await asyncio.wait_for(page.title(), timeout=PAGE_TITLE_TIMEOUT_S)
		except Exception:
			title = "(Unavailable)"
		return f"Switched to tab [{target}]: '{title}' ({page.url})"
	except asyncio.TimeoutError:
		return f"Error switching tab [{target}]: Timed out"
	except Exception as exc:
		return f"Error switching tab: {exc}"


@mcp.tool()
@_with_tool_deadline
async def close_tab(
	tab: Union[int, str, None] = None,
	index: Optional[int] = None,
	ctx: Optional[Context] = None,
) -> str:
	"""Close tab by index, browser-derived id, or name substring (-1 active, -99 last). Accepts 'tab'/'index'."""
	target = tab if tab is not None else (index if index is not None else -1)
	try:
		client_key = _session_key(ctx)
		if target == -99:
			return await close_browser(ctx=ctx) if ctx is not None else await close_browser()

		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		await session.sync_tab_ids(pages)
		if not pages:
			return await close_browser(ctx=ctx) if ctx is not None else await close_browser()

		if isinstance(target, str):
			norm = target.strip().lower()
			resolved = next((i for i, p in enumerate(pages) if session.get_tab_id(p) == norm), -1)
			if resolved < 0:
				resolved = await _resolve_tab_name(session, pages, norm)
			if resolved < 0:
				hint = session._format_open_tabs_hint(pages)
				return f"No open tab with id or name '{norm}'.{hint or ' Use status() to list open tabs.'}"
			target = resolved

		if target == -1:
			active = session.get_active_page_for(client_key)
			if active is not None:
				page = active
			else:
				page = pages[-1]
			target_index = pages.index(page) if page in pages else -1
		else:
			if target < 0 or target >= len(pages):
				hint = session._format_open_tabs_hint(pages)
				return f"Invalid tab index {target}. Total open tabs: {len(pages)}.{hint}"
			page = pages[target]
			target_index = target

		url = page.url
		tab_id = session.get_tab_id(page)
		was_active = page == session.get_active_page_for(client_key)
		try:
			await asyncio.wait_for(keep_alive_mgr.stop(page), timeout=CDP_OP_TIMEOUT_S)
		except Exception:
			pass
		try:
			await asyncio.wait_for(page.close(), timeout=PAGE_CLOSE_TIMEOUT_S)
		except asyncio.TimeoutError:
			probe_ok = await asyncio.wait_for(asyncio.to_thread(session._probe_cdp_ready), timeout=2.0)
			if probe_ok:
				return f"Error closing tab [{target_index}]: Timed out after {int(PAGE_CLOSE_TIMEOUT_S * 1000)}ms (renderer unresponsive; browser still reachable, try restart_browser)"
			try:
				await asyncio.wait_for(session.close(), timeout=5.0)
			except Exception:
				pass
			try:
				await asyncio.wait_for(asyncio.to_thread(_force_kill_browser_processes), timeout=10.0)
			except Exception:
				pass
			return f"Error closing tab [{target_index}]: renderer unresponsive and CDP endpoint dead — wedged browser force-restarted; the next call relaunches it, reopen the tab and re-navigate."

		remaining = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		if was_active:
			session.set_active_page(remaining[-1] if remaining else None, client_key=client_key)
		if not remaining:
			if ctx is not None:
				await close_browser(ctx=ctx)
			else:
				await close_browser()
			return f"Closed last tab [{target_index}] ({tab_id}) ({url}). Browser window completely closed."
		return f"Closed tab [{target_index}] ({tab_id}) ({url}). Remaining open tabs: {len(remaining)}"
	except asyncio.TimeoutError:
		return f"Error closing tab [{target}]: Timed out"
	except Exception as exc:
		return f"Error closing tab: {exc}"


def _force_kill_browser_processes():
	"""Terminates lingering Chromium processes strictly associated with the agent's profile or CDP port."""
	try:
		r1 = subprocess.run(["pkill", "-f", "--", f"--user-data-dir={PROFILE_DIR}"], capture_output=True)
		r2 = subprocess.run(["pkill", "-f", "--", f"--remote-debugging-port={CDP_PORT}"], capture_output=True)
		if r1.returncode == 0 or r2.returncode == 0:
			time.sleep(0.3)
			subprocess.run(["pkill", "-9", "-f", "--", f"--user-data-dir={PROFILE_DIR}"], capture_output=True)
			subprocess.run(["pkill", "-9", "-f", "--", f"--remote-debugging-port={CDP_PORT}"], capture_output=True)
		for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
			lock_path = os.path.join(PROFILE_DIR, lock_name)
			if os.path.islink(lock_path) or os.path.exists(lock_path):
				try:
					os.unlink(lock_path)
				except Exception:
					pass
	except Exception:
		pass


async def _close_browser_inner(force_kill: bool = True) -> str:
	cdp_timed_out = False
	if session._browser is not None and session._browser.is_connected():
		try:
			cdp = await asyncio.wait_for(
				session._browser.new_browser_cdp_session(),
				timeout=BROWSER_CLOSE_TIMEOUT_S,
			)
			await asyncio.wait_for(cdp.send("Browser.close"), timeout=BROWSER_CLOSE_TIMEOUT_S)
		except asyncio.TimeoutError:
			cdp_timed_out = True
		except Exception:
			pass
	if not cdp_timed_out and session._context is not None:
		try:
			await asyncio.wait_for(session._context.close(), timeout=BROWSER_CLOSE_TIMEOUT_S)
		except Exception:
			pass
	if not cdp_timed_out and session._browser is not None:
		try:
			await asyncio.wait_for(session._browser.close(), timeout=BROWSER_CLOSE_TIMEOUT_S)
		except Exception:
			pass
	try:
		await asyncio.wait_for(session.close(), timeout=BROWSER_CLOSE_TIMEOUT_S)
	except Exception:
		session.close_sync()
	_save_persisted_headless(None)
	if force_kill:
		try:
			await asyncio.wait_for(asyncio.to_thread(_force_kill_browser_processes), timeout=10.0)
		except Exception:
			pass
	return "Browser completely closed and all resources released."


@mcp.tool()
@_with_tool_deadline
async def close_browser(
	force_kill: bool = True,
	force: bool = False,
	ctx: Optional[Context] = None,
) -> str:
	"""Completely close the browser window, terminate all browser and renderer processes, and release all resources."""
	try:
		client_key = _session_key(ctx)
		if _in_lifecycle_op.get():
			return await _close_browser_inner(force_kill=force_kill)
		async with session._lifecycle_lock:
			refusal = _check_sibling_sessions(
				"close the browser",
				client_key,
				force=force,
				force_hint="close_browser(force=True)",
			)
			if refusal is not None:
				return refusal
			token = _in_lifecycle_op.set(True)
			try:
				return await _close_browser_inner(force_kill=force_kill)
			finally:
				_in_lifecycle_op.reset(token)
	except Exception as exc:
		return f"Error closing browser: {exc}"


async def _restart_browser_inner(
	headless: Optional[bool] = None,
	url: Optional[str] = None,
	client_key: str = "default",
) -> str:
	preserved_urls: list[str] = []
	try:
		pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		for p in pages:
			if not p.is_closed():
				curr = p.url
				if curr and curr != "about:blank" and not curr.startswith("chrome://") and not curr.startswith("chrome-error://"):
					preserved_urls.append(curr)
	except Exception:
		pass

	if not preserved_urls:
		try:
			active = session.get_active_page_for(client_key)
			if active is not None:
				curr = active.url
				if curr and curr != "about:blank" and not curr.startswith("chrome://") and not curr.startswith("chrome-error://"):
					preserved_urls.append(curr)
		except Exception:
			pass

	target_url = url if url is not None else (preserved_urls[0] if preserved_urls else None)
	extra_urls = preserved_urls if url is not None else preserved_urls[1:]

	if headless is not None:
		session.headless = bool(headless)

	await close_browser(force_kill=True)
	_save_persisted_headless(session.headless)
	await asyncio.sleep(0.3)
	page = await session.get_active_page(client_key=client_key)
	if target_url:
		try:
			await asyncio.wait_for(
				page.goto(target_url, timeout=15000, wait_until="domcontentloaded"),
				timeout=18.0,
			)
		except Exception:
			pass
	ctx_browser = await asyncio.wait_for(session.get_context(), timeout=GET_CONTEXT_TIMEOUT_S)
	urls_to_reopen = [u for u in extra_urls if not (url is not None and u == url)]
	if urls_to_reopen:
		reopen_pairs: list[tuple[Page, str]] = []
		for extra_url in urls_to_reopen:
			try:
				new_page = await session.new_background_page(ctx_browser)
				reopen_pairs.append((new_page, extra_url))
			except Exception:
				pass

		async def _load_extra(extra_page: Page, extra_url: str) -> None:
			try:
				await _apply_animations_to_page(extra_page)
				await network_blocker.ensure_page(extra_page)
				await asyncio.wait_for(
					extra_page.goto(extra_url, timeout=10000, wait_until="domcontentloaded"),
					timeout=12.0,
				)
			except Exception:
				pass

		if reopen_pairs:
			await asyncio.gather(*(_load_extra(p, u) for p, u in reopen_pairs))
	try:
		title = await asyncio.wait_for(page.title(), timeout=PAGE_TITLE_TIMEOUT_S)
	except Exception:
		title = "(Unavailable)"
	mode_str = "Headless" if session.headless else "Headed"
	reopened = f" (reopened {len(preserved_urls)} tab(s))" if preserved_urls and url is None else ""
	return f"Browser restarted successfully in {mode_str} mode on tab [0]: '{title}' ({page.url}){reopened}"


@mcp.tool()
@_with_tool_deadline
async def restart_browser(
	headless: Optional[bool] = None,
	url: Optional[str] = None,
	force: bool = False,
	ctx: Optional[Context] = None,
) -> str:
	"""Restart Chromium cleanly, preserving non-blank tabs unless url overrides tab [0]. headless=None keeps current mode."""
	try:
		client_key = _session_key(ctx)
		if _in_lifecycle_op.get():
			return await asyncio.wait_for(
				_restart_browser_inner(headless=headless, url=url, client_key=client_key),
				timeout=RESTART_BROWSER_TIMEOUT_S,
			)
		async with session._lifecycle_lock:
			refusal = _check_sibling_sessions(
				"restart the browser",
				client_key,
				force=force,
				force_hint=f"restart_browser(headless={headless}, force=True)",
			)
			if refusal is not None:
				return refusal
			token = _in_lifecycle_op.set(True)
			try:
				return await asyncio.wait_for(
					_restart_browser_inner(headless=headless, url=url, client_key=client_key),
					timeout=RESTART_BROWSER_TIMEOUT_S,
				)
			finally:
				_in_lifecycle_op.reset(token)
	except asyncio.TimeoutError:
		return f"Error restarting browser: Timed out after {int(RESTART_BROWSER_TIMEOUT_S)}s"
	except Exception as exc:
		return f"Error restarting browser: {exc}"


@mcp.tool()
@_with_tool_deadline
async def set_headless(
	headless: bool = True,
	force: bool = False,
	ctx: Optional[Context] = None,
) -> str:
	"""Switch headed (default) vs headless mode; restarts Chromium on change (switching to headless refuses if non-blank tabs exist unless force=True). Prefer wait_for_human() for login/2FA."""
	client_key = _session_key(ctx)
	async with session._lifecycle_lock:
		pages: list[Page] = []
		try:
			pages = await asyncio.wait_for(session.get_pages(), timeout=CDP_OP_TIMEOUT_S)
		except Exception:
			pages = []

		try:
			detected_headless = await asyncio.wait_for(
				asyncio.to_thread(session._detect_cdp_headless),
				timeout=CDP_OP_TIMEOUT_S,
			)
			if detected_headless is not None:
				session.headless = detected_headless
		except Exception:
			pass

		if session.headless == headless and (session._context is not None or session._browser is not None):
			mode_str = "Headless" if headless else "Headed"
			return f"Browser is already running in {mode_str} mode."

		refusal = _check_sibling_sessions(
			"restart the browser to switch mode",
			client_key,
			force=force,
			force_hint=f"set_headless(headless={headless}, force=True)",
		)
		if refusal is not None:
			return refusal

		active_tabs = [
			(idx, p.url)
			for idx, p in enumerate(pages)
			if not p.is_closed() and p.url and p.url != "about:blank" and not p.url.startswith("chrome://")
		]
		if headless and active_tabs and not force:
			target_mode = "Headless" if headless else "Headed"
			tab_list = "\n".join(f"\t[{idx}] {tab_url}" for idx, tab_url in active_tabs)
			return (
				f"Refusing to restart browser to switch to {target_mode} mode because {len(active_tabs)} "
				f"tab(s) are currently open (switching modes restarts Chromium and may disrupt active logins/sessions):\n"
				f"{tab_list}\n"
				f"Keep using the current browser mode, ask the user before interrupting, or call "
				f"set_headless(headless={headless}, force=True) to restart and reopen these URLs."
			)

		token = _in_lifecycle_op.set(True)
		try:
			return await (
				restart_browser(headless=headless, ctx=ctx)
				if ctx is not None
				else restart_browser(headless=headless)
			)
		finally:
			_in_lifecycle_op.reset(token)


def _install_browser() -> int:
	from patchright.__main__ import main as patchright_main

	print(f"Installing Patchright Chromium (no headless shell) for {__version__}...")
	sys.argv = ["patchright", "install", "chromium", "--no-shell", "--no-progress", "--no-remove"]
	try:
		patchright_main()
	except SystemExit as exc:
		return int(exc.code or 0)
	return 0


def _doctor(smoke: bool = False) -> int:
	checks = []

	python_ok = sys.version_info >= (3, 11)
	checks.append(("Python >= 3.11", python_ok, sys.version.split()[0]))

	try:
		from importlib.metadata import version as _dist_version

		deps = {name: _dist_version(name) for name in ("mcp", "patchright")}
		deps_ok = deps["mcp"] == "1.30.0"
		checks.append(("Dependencies", deps_ok, ", ".join(f"{k}={v}" for k, v in deps.items())))
	except Exception as exc:
		checks.append(("Dependencies", False, str(exc)))

	cheatsheet_ok = os.path.isfile(_CHEATSHEET_FILE)
	checks.append(("Cheat-sheet written", cheatsheet_ok, _CHEATSHEET_FILE))

	cdp_running = session._probe_cdp_ready()
	checks.append((f"CDP endpoint ({CDP_URL})", cdp_running, "reachable" if cdp_running else "not running (will launch on first browser call)"))

	if cdp_running:
		from patchright.async_api import async_playwright

		async def _probe():
			async with async_playwright() as pw:
				browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=1500)
				return len(browser.contexts[0].pages) if browser.contexts else 0

		try:
			tabs = asyncio.run(_probe())
			checks.append(("CDP attach", True, f"{tabs} tab(s) visible"))
		except Exception as exc:
			checks.append(("CDP attach", False, str(exc)))
	else:
		async def _probe_executable():
			from patchright.async_api import async_playwright as _async_playwright

			async with _async_playwright() as pw:
				return pw.chromium.executable_path

		try:
			exe = asyncio.run(_probe_executable())
			browser_ok = os.path.isfile(exe) or bool(BROWSER_EXECUTABLE and os.path.isfile(BROWSER_EXECUTABLE))
			checks.append(("Browser executable", browser_ok, exe))
		except Exception as exc:
			checks.append(("Browser executable", False, str(exc)))

	if smoke and not cdp_running:
		try:
			page = asyncio.run(session.get_active_page())
			title = asyncio.run(page.title())
			checks.append(("Browser smoke test", True, f"active tab: {title}"))
			session.close_sync()
		except Exception as exc:
			checks.append(("Browser smoke test", False, str(exc)))

	informational = {f"CDP endpoint ({CDP_URL})", "Browser executable"}
	width = max(len(label) for label, _, _ in checks)
	failures = 0
	for label, ok, detail in checks:
		status = "OK" if ok else "FAIL"
		if not ok and label not in informational:
			failures += 1
		if not ok and label in informational:
			status = "INFO"
		print(f"[{status}] {label.ljust(width)}  {detail}")

	if failures:
		print(f"\n{failures} check(s) failed. Run 'stealth-browser --install-browser' to install Chromium.")
		return 1
	print("\nAll checks passed. stealth-browser is ready.")
	return 0


class _DaemonState:
	def __init__(self, script_hash: str):
		self.script_hash = script_hash
		self.started_at = time.monotonic()
		self.last_activity = self.started_at
		self.in_flight = 0
		self.servers: list[Any] = []

	def touch(self) -> None:
		self.last_activity = time.monotonic()

	def request_stop(self) -> None:
		for srv in self.servers:
			srv.should_exit = True


def _asgi_header(scope: dict, name: bytes) -> Optional[str]:
	target = name.lower()
	for k, v in scope.get("headers") or ():
		if k.lower() == target:
			return v.decode("latin-1")
	return None


async def _asgi_send_json(send, status_code: int, payload: dict, extra_headers: Optional[list[tuple[bytes, bytes]]] = None) -> None:
	body = json.dumps(payload).encode("utf-8")
	headers = [
		(b"content-type", b"application/json"),
		(b"content-length", str(len(body)).encode("ascii")),
	]
	if extra_headers:
		headers.extend(extra_headers)
	await send({"type": "http.response.start", "status": status_code, "headers": headers})
	await send({"type": "http.response.body", "body": body})


class _DaemonUdsASGI:
	def __init__(self, base_app, state: _DaemonState, mcp_app: FastMCP, lifespan_cm=None):
		self.base_app = base_app
		self.state = state
		self.mcp_app = mcp_app
		self.lifespan_cm = lifespan_cm

	async def __call__(self, scope, receive, send):
		if scope["type"] == "lifespan" and self.lifespan_cm is not None:
			await self._handle_lifespan(scope, receive, send)
			return
		if scope["type"] == "http":
			path = scope.get("path", "")
			method = (scope.get("method") or "GET").upper()
			if path == "/_daemon/health" and method == "GET":
				sm = getattr(self.mcp_app, "_session_manager", None)
				active_sessions = len(getattr(sm, "_server_instances", {})) if sm is not None else 0
				await _asgi_send_json(
					send,
					200,
					{
						"ok": True,
						"pid": os.getpid(),
						"version": __version__,
						"script_hash": self.state.script_hash,
						"active_sessions": active_sessions,
						"in_flight": self.state.in_flight,
						"uptime_s": round(time.monotonic() - self.state.started_at, 2),
					},
				)
				return
			if path == "/_daemon/stop" and method == "POST":
				self.state.request_stop()
				await _asgi_send_json(send, 200, {"stopping": True})
				return
			self.state.in_flight += 1
			self.state.touch()
			sid = _asgi_header(scope, b"mcp-session-id") if method == "DELETE" else None
			try:
				await self.base_app(scope, receive, send)
			finally:
				self.state.in_flight = max(0, self.state.in_flight - 1)
				self.state.touch()
				if method == "DELETE" and sid:
					session.clear_client(f"http:{sid}")
			return
		await self.base_app(scope, receive, send)

	async def _handle_lifespan(self, scope, receive, send):
		while True:
			message = await receive()
			if message["type"] == "lifespan.startup":
				try:
					self._cm = self.lifespan_cm(self.base_app)
					await self._cm.__aenter__()
				except Exception as exc:
					await send({"type": "lifespan.startup.failed", "message": str(exc)})
					return
				await send({"type": "lifespan.startup.complete"})
			elif message["type"] == "lifespan.shutdown":
				try:
					await self._cm.__aexit__(None, None, None)
				except Exception as exc:
					await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
					return
				await send({"type": "lifespan.shutdown.complete"})
				return


class _BearerAuthASGI:
	def __init__(self, base_app, expected_token: str, state: Optional[_DaemonState] = None):
		self.base_app = base_app
		self.expected_token = expected_token
		self.state = state

	async def __call__(self, scope, receive, send):
		if scope["type"] == "http":
			auth_header = _asgi_header(scope, b"authorization") or ""
			scheme, _, provided = auth_header.partition(" ")
			if scheme.lower() != "bearer" or not provided or not hmac.compare_digest(provided, self.expected_token):
				await _asgi_send_json(
					send,
					401,
					{"error": "Unauthorized"},
					extra_headers=[(b"www-authenticate", b"Bearer")],
				)
				return
			method = (scope.get("method") or "GET").upper()
			sid = _asgi_header(scope, b"mcp-session-id") if method == "DELETE" else None
			if self.state is not None:
				self.state.in_flight += 1
				self.state.touch()
			try:
				await self.base_app(scope, receive, send)
			finally:
				if self.state is not None:
					self.state.in_flight = max(0, self.state.in_flight - 1)
					self.state.touch()
				if method == "DELETE" and sid:
					session.clear_client(f"http:{sid}")
			return
		await self.base_app(scope, receive, send)


def _serve_daemon(
	http_addr: Optional[str] = None,
	idle_exit_s: int = DAEMON_DEFAULT_IDLE_EXIT,
	runtime_dir: Optional[str] = None,
	token_path: Optional[str] = None,
) -> int:
	import uvicorn

	http_target: Optional[tuple[str, int]] = None
	bearer_token: Optional[str] = None
	if http_addr is not None:
		try:
			http_target = _parse_loopback_http(http_addr)
		except ValueError as exc:
			print(f"Error: {exc}", file=sys.stderr)
			return 2
		bearer_token = _ensure_daemon_token(token_path)

	paths = _daemon_paths(runtime_dir=runtime_dir, token_path=token_path)
	rdir = paths["runtime_dir"]
	sock_path = paths["sock_path"]
	meta_path = paths["meta_path"]
	_ensure_secure_runtime_dir(rdir)

	if _probe_daemon_health(sock_path, timeout=0.5) is not None:
		print(f"Daemon already running at {sock_path}", file=sys.stderr)
		return 1
	if os.path.exists(sock_path):
		try:
			os.unlink(sock_path)
		except OSError:
			pass

	mcp._session_manager = None
	base_app = mcp.streamable_http_app()
	state = _DaemonState(_script_sha256())

	@contextlib.asynccontextmanager
	async def _daemon_lifespan(app):
		async with _lifespan(app):
			async with mcp.session_manager.run():
				if os.path.exists(sock_path):
					try:
						os.chmod(sock_path, 0o600)
					except OSError:
						pass
				meta_payload = {
					"pid": os.getpid(),
					"version": __version__,
					"script_hash": state.script_hash,
					"uds": sock_path,
					"http": f"{http_target[0]}:{http_target[1]}" if http_target else None,
				}
				fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
				try:
					os.write(fd, json.dumps(meta_payload).encode("utf-8"))
				finally:
					os.close(fd)

				idle_task: Optional[asyncio.Task] = None
				if idle_exit_s > 0:
					async def _idle_exit_loop():
						poll = max(0.05, min(30.0, idle_exit_s / 4.0))
						while True:
							await asyncio.sleep(poll)
							sm = getattr(mcp, "_session_manager", None)
							active_sessions = len(getattr(sm, "_server_instances", {})) if sm is not None else 0
							if (
								state.in_flight == 0
								and active_sessions == 0
								and not keep_alive_mgr.has_active()
								and (time.monotonic() - state.last_activity) >= idle_exit_s
							):
								state.request_stop()
								return

					idle_task = asyncio.create_task(_idle_exit_loop())

				try:
					yield
				finally:
					if idle_task is not None and not idle_task.done():
						idle_task.cancel()
					try:
						await asyncio.wait_for(session.close(), timeout=5.0)
					except Exception:
						session.close_sync()
					for p in (sock_path, meta_path):
						if os.path.exists(p):
							try:
								os.unlink(p)
							except OSError:
								pass

	uds_app = _DaemonUdsASGI(base_app, state, mcp, lifespan_cm=_daemon_lifespan)
	uds_cfg = uvicorn.Config(uds_app, log_level="warning", lifespan="on")
	uds_server = uvicorn.Server(uds_cfg)
	state.servers.append(uds_server)

	http_server = None
	if http_target is not None and bearer_token is not None:
		http_app = _BearerAuthASGI(base_app, bearer_token, state=state)
		http_cfg = uvicorn.Config(http_app, host=http_target[0], port=http_target[1], log_level="warning", lifespan="off")
		http_server = uvicorn.Server(http_cfg)
		state.servers.append(http_server)

	for srv in state.servers:
		srv.install_signal_handlers = lambda: None

	old_umask = os.umask(0o177)
	uds_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	try:
		uds_sock.bind(sock_path)
		os.chmod(sock_path, 0o600)
		uds_sock.listen(128)
		uds_sock.setblocking(False)
	except Exception:
		uds_sock.close()
		os.umask(old_umask)
		raise
	finally:
		os.umask(old_umask)

	async def _run_all():
		loop = asyncio.get_running_loop()
		for sig in (signal.SIGTERM, signal.SIGINT):
			try:
				loop.add_signal_handler(sig, state.request_stop)
			except NotImplementedError:
				pass
		coros = [uds_server.serve(sockets=[uds_sock])]
		if http_server is not None:
			coros.append(http_server.serve())
		await asyncio.gather(*coros)

	try:
		asyncio.run(_run_all())
	except (KeyboardInterrupt, SystemExit):
		pass
	finally:
		try:
			uds_sock.close()
		except Exception:
			pass
		session.close_sync()
		for p in (sock_path, meta_path):
			if os.path.exists(p):
				try:
					os.unlink(p)
				except OSError:
					pass
	return 0


def _apply_tool_gating(
	disabled_tools: frozenset[str],
	gating_notice: str = "",
	mcp_server: Optional[FastMCP] = None,
) -> list[str]:
	target = mcp if mcp_server is None else mcp_server
	removed: list[str] = []
	effective = frozenset(disabled_tools) - PROTECTED_TOOLS
	for name in sorted(effective):
		try:
			target.remove_tool(name)
			removed.append(name)
		except Exception:
			pass
	if gating_notice:
		existing = target._mcp_server.instructions or ""
		if gating_notice not in existing:
			target._mcp_server.instructions = f"{existing}\n{gating_notice}" if existing else gating_notice
	return removed


def main():
	args = _build_arg_parser().parse_args()

	if args.list_sections:
		print(_format_sections_list())
		raise SystemExit(0)
	if args.install_browser:
		raise SystemExit(_install_browser())
	if args.doctor or args.smoke:
		raise SystemExit(_doctor(smoke=args.smoke))
	if args.daemon_status:
		raise SystemExit(_run_daemon_status())
	if args.daemon_stop:
		raise SystemExit(_run_daemon_stop())

	try:
		disabled_tools, disabled_sections, effective_minimal = _resolve_disabled_tools(
			minimal=args.minimal,
			disabled_sections=_disabled_sections_from_args(args),
		)
	except ValueError as exc:
		print(f"Error: {exc}", file=sys.stderr)
		raise SystemExit(2)
	notice = _gating_notice(effective_minimal, disabled_sections, disabled_tools)

	if args.serve:
		if disabled_tools:
			_apply_tool_gating(disabled_tools, notice)
		raise SystemExit(_serve_daemon(http_addr=args.http, idle_exit_s=args.daemon_idle_exit))
	if not args.standalone:
		raise SystemExit(
			_run_stdio_proxy(
				idle_exit_s=args.daemon_idle_exit,
				disabled_tools=disabled_tools,
				gating_notice=notice,
			)
		)

	if disabled_tools:
		_apply_tool_gating(disabled_tools, notice)

	def _handle_signal(signum, _frame):
		raise SystemExit(0)

	for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
		signal.signal(sig, _handle_signal)

	try:
		mcp.run()
	except (KeyboardInterrupt, SystemExit):
		pass
	finally:
		session.close_sync()
		os._exit(0)


if __name__ == "__main__":
	main()

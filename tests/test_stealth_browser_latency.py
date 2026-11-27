import asyncio
import importlib.machinery
import importlib.util
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_sb():
	script_path = Path(__file__).resolve().parent.parent / "bin" / "stealth-browser"
	loader = importlib.machinery.SourceFileLoader("stealth_browser_latency_mod", str(script_path))
	spec = importlib.util.spec_from_loader(loader.name, loader)
	mod = importlib.util.module_from_spec(spec)
	loader.exec_module(mod)
	return mod


sb = _load_sb()


class SlowTitlePage:
	def __init__(self, url: str, title: str, delay: float = 0.0, fail: bool = False):
		self.url = url
		self._title = title
		self._delay = delay
		self._fail = fail
		self._closed = False
		self._listeners: dict[str, list] = {}

	def is_closed(self) -> bool:
		return self._closed

	def on(self, event: str, cb) -> None:
		self._listeners.setdefault(event, []).append(cb)

	async def title(self) -> str:
		if self._delay:
			await asyncio.sleep(self._delay)
		if self._fail:
			raise RuntimeError("renderer busy")
		return self._title


def test_status_fetches_tab_titles_in_parallel_preserving_order(monkeypatch):
	async def _run():
		pages = [
			SlowTitlePage("https://a.example", "Alpha", delay=0.08),
			SlowTitlePage("https://b.example", "Beta", delay=0.08, fail=True),
			SlowTitlePage("https://c.example", "Gamma", delay=0.08),
			SlowTitlePage("https://d.example", "Delta", delay=0.08),
		]
		mgr = sb.BrowserSessionManager("http://127.0.0.1:9222")
		mgr._active_page = pages[2]
		mgr.get_pages = AsyncMock(return_value=pages)
		monkeypatch.setattr(sb, "session", mgr)

		t0 = time.perf_counter()
		out = await sb.status()
		elapsed = time.perf_counter() - t0

		assert elapsed < 0.25, f"Expected parallel title fetches (<0.25s), got {elapsed:.3f}s"
		lines = [ln for ln in out.splitlines() if ln.startswith("\t[")]
		assert len(lines) == 4
		assert "[0 | t1 | alpha] Alpha - https://a.example" in lines[0]
		assert "[1 | t2 | unresponsive-busy] (Unresponsive / Busy) - https://b.example" in lines[1]
		assert "[2 | t3 | gamma] Gamma - https://c.example (ACTIVE in this session)" in lines[2]
		assert "[3 | t4 | delta] Delta - https://d.example" in lines[3]

	asyncio.run(_run())


def test_get_content_clean_skips_locator_count_and_handles_missing(monkeypatch):
	async def _run():
		page = MagicMock()
		page.url = "https://example.test/doc"
		page.locator = MagicMock()
		page.evaluate = AsyncMock(return_value=None)
		monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page))

		missing = await sb.get_content(selector="#absent", clean=True)
		assert missing == "Element '#absent' not found on page https://example.test/doc"
		page.locator.assert_not_called()

		page.evaluate = AsyncMock(return_value="Clean Heading\n\nBody paragraph")
		found = await sb.get_content(selector="main", clean=True)
		assert found == "Clean Heading\n\nBody paragraph"
		page.locator.assert_not_called()

		raw_loc = MagicMock()
		raw_loc.first = raw_loc
		raw_loc.count = AsyncMock(return_value=0)
		page.locator = MagicMock(return_value=raw_loc)
		missing_raw = await sb.get_content(selector="#absent", clean=False)
		assert missing_raw == "Element '#absent' not found on page https://example.test/doc"
		raw_loc.count.assert_awaited_once()

	asyncio.run(_run())


def test_get_content_clean_js_returns_null_and_caches_computed_style():
	assert "if (!el) return null;" in sb._GET_CONTENT_CLEAN_JS
	assert "if (cs === null)" in sb._GET_CONTENT_CLEAN_JS


def test_animations_frozen_does_not_reinject_on_every_resolve_page(monkeypatch):
	async def _run():
		listeners: dict[str, list] = {}

		class AnimPage:
			def __init__(self):
				self.url = "https://example.test/spa"
				self.emulate_media = AsyncMock()
				self.evaluate = AsyncMock()

			def is_closed(self) -> bool:
				return False

			def on(self, event: str, cb) -> None:
				listeners.setdefault(event, []).append(cb)

		page = AnimPage()
		mgr = sb.BrowserSessionManager("http://127.0.0.1:9222")
		mgr.animations = False
		mgr._active_page = page
		mgr._browser = MagicMock()
		mgr._browser.is_connected.return_value = True
		monkeypatch.setattr(sb, "session", mgr)

		await sb._apply_animations_to_page(page)
		assert page.emulate_media.await_count == 1
		assert page.evaluate.await_count == 1

		for _ in range(5):
			await mgr.resolve_page(-1)
		await asyncio.sleep(0)
		await asyncio.sleep(0)
		assert page.emulate_media.await_count == 1
		assert page.evaluate.await_count == 1

		for cb in listeners.get("domcontentloaded", []):
			cb(page)
		await asyncio.sleep(0)
		await asyncio.sleep(0)
		assert page.evaluate.await_count == 2

	asyncio.run(_run())


def test_click_and_type_ref_skip_count_on_single_frame_and_force_uses_single_eval(monkeypatch):
	async def _run():
		class EvalLocator:
			def __init__(self, count: int = 1):
				self._count = count
				self.count = AsyncMock(return_value=count)
				self.click = AsyncMock()
				self.fill = AsyncMock()
				self.evaluate = AsyncMock()
				self.element_handle = AsyncMock()

			@property
			def first(self):
				return self

		loc = EvalLocator(count=1)
		page = MagicMock()
		page.url = "https://example.test/form"
		page.main_frame = page
		page.frames = [page]
		page._dialog_history = []
		page.locator = MagicMock(return_value=loc)
		page.evaluate = AsyncMock(return_value=True)
		monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page))

		res_click = await sb.click(ref="e5")
		assert "Clicked element matching ref 'e5'" in res_click
		loc.count.assert_not_awaited()
		loc.click.assert_awaited_once_with(timeout=2000)

		res_force = await sb.click(ref="e5", force=True, timeout_ms=2500)
		assert "Clicked element matching ref 'e5' (force)" in res_force
		loc.count.assert_not_awaited()
		loc.element_handle.assert_not_awaited()
		loc.evaluate.assert_awaited_once_with("el => el.click()", timeout=2500)
		assert loc.click.await_count == 1

		res_batch = await sb.batch([{"action": "click", "ref": "e5", "force": True, "timeout_ms": 3000}])
		assert "[0] click ref 'e5' (force): OK" in res_batch
		loc.element_handle.assert_not_awaited()
		assert loc.evaluate.await_count == 2
		assert loc.evaluate.call_args.kwargs == {"timeout": 3000}

		res_type = await sb.type(ref="e5", text="hello")
		assert "Typed text into ref 'e5'" in res_type
		loc.count.assert_not_awaited()
		loc.fill.assert_awaited_once_with("hello", timeout=5000)

		missing_loc = EvalLocator(count=0)
		missing_loc.fill = AsyncMock(side_effect=RuntimeError("not found"))
		page.locator = MagicMock(return_value=missing_loc)
		res_fallback = await sb.type(ref="e9", text="shadow")
		assert "Typed text into ref 'e9'" in res_fallback
		missing_loc.count.assert_awaited_once()
		page.evaluate.assert_awaited_once()

	asyncio.run(_run())


def test_snapshot_js_filters_before_visibility_and_caps_visible_iframe_poll():
	assert "matchesFilter(node) && isVisible(node)" in sb._SNAPSHOT_JS
	assert "if (!isVisible(f)) return false;" in sb._SNAPSHOT_JS
	assert "Date.now() + 400" in sb._SNAPSHOT_JS
	assert "Date.now() + 1500" not in sb._SNAPSHOT_JS
	assert "const candidates = [];" not in sb._SNAPSHOT_JS


class FakeCDPSession:
	def __init__(self):
		self.calls: list[tuple[str, dict | None]] = []
		self.listeners: dict[str, list] = {}
		self.detached = False

	def on(self, event: str, cb) -> None:
		self.listeners.setdefault(event, []).append(cb)

	def emit(self, event: str, payload: dict) -> None:
		for cb in list(self.listeners.get(event, [])):
			cb(payload)

	async def send(self, method: str, params: dict | None = None):
		self.calls.append((method, params))
		return {}

	async def detach(self):
		self.detached = True


class FakeCDPPage:
	def __init__(self, cdp: FakeCDPSession):
		self.url = "https://example.test/page"
		self._closed = False
		self._listeners: dict[str, list] = {}
		self.context = MagicMock()
		self.context.new_cdp_session = AsyncMock(return_value=cdp)

	def is_closed(self) -> bool:
		return self._closed

	def on(self, event: str, cb) -> None:
		self._listeners.setdefault(event, []).append(cb)

	def emit(self, event: str) -> None:
		for cb in list(self._listeners.get(event, [])):
			cb(self)


def test_ensure_interceptor_does_not_register_context_route():
	async def _run():
		mgr = sb.BrowserSessionManager("http://127.0.0.1:9222")
		mgr._configure_native_downloads = AsyncMock()
		mgr._ensure_animation_init_script = AsyncMock()
		fake_ctx = MagicMock()
		fake_ctx.route = AsyncMock()

		await mgr._ensure_interceptor(fake_ctx)
		fake_ctx.route.assert_not_awaited()
		assert getattr(fake_ctx, "_interceptor_attached", False) is True

	asyncio.run(_run())


def test_network_blocker_media_toggle_fetch_fail_and_close_cleanup():
	async def _run():
		cdp = FakeCDPSession()
		page = FakeCDPPage(cdp)
		blocker = sb.NetworkBlockManager(block_trackers=True)

		ok = await blocker.set_media_blocked(page, True)
		assert ok is True
		assert page in blocker._sessions
		assert page in blocker._media_enabled
		methods = [m for m, _ in cdp.calls]
		assert methods == ["Network.enable", "Network.setBlockedURLs", "Fetch.enable"]
		blocked_urls = cdp.calls[1][1]["urls"]
		assert "*.png" in blocked_urls
		assert "*.png?*" in blocked_urls
		assert "*://*.google-analytics.com/*" in blocked_urls
		assert cdp.calls[2][1]["patterns"] == list(sb.MEDIA_FETCH_PATTERNS)

		cdp.emit("Fetch.requestPaused", {"requestId": "req-42"})
		await asyncio.sleep(0)
		await asyncio.sleep(0)
		assert ("Fetch.failRequest", {"requestId": "req-42", "errorReason": "BlockedByClient"}) in cdp.calls
		assert not blocker._pending_tasks

		cdp.calls.clear()
		ok2 = await blocker.set_media_blocked(page, False)
		assert ok2 is True
		assert page in blocker._sessions
		assert page not in blocker._media_enabled
		assert [m for m, _ in cdp.calls] == ["Network.setBlockedURLs", "Fetch.disable"]
		urls_after = cdp.calls[0][1]["urls"]
		assert "*.png" not in urls_after
		assert "*://*.google-analytics.com/*" in urls_after

		page.emit("close")
		await asyncio.sleep(0)
		await asyncio.sleep(0)
		assert cdp.detached is True
		assert page not in blocker._sessions

	asyncio.run(_run())


def test_network_blocker_zero_session_when_trackers_disabled():
	async def _run():
		cdp = FakeCDPSession()
		page = FakeCDPPage(cdp)
		blocker = sb.NetworkBlockManager(block_trackers=False)

		assert await blocker.ensure_page(page) is False
		page.context.new_cdp_session.assert_not_awaited()
		assert page not in blocker._sessions

		assert await blocker.set_media_blocked(page, True) is True
		page.context.new_cdp_session.assert_awaited_once()
		assert page in blocker._media_enabled

		assert await blocker.set_media_blocked(page, False) is True
		assert cdp.detached is True
		assert page not in blocker._sessions
		assert page not in blocker._media_enabled

	asyncio.run(_run())


def test_network_blocker_close_all_and_set_page_media_state(monkeypatch):
	async def _run():
		cdp = FakeCDPSession()
		page = FakeCDPPage(cdp)
		blocker = sb.NetworkBlockManager(block_trackers=True)
		monkeypatch.setattr(sb, "network_blocker", blocker)

		await sb.set_page_media_state(page, block_media=True)
		assert getattr(page, "_media_blocked", False) is True
		assert page in blocker._media_enabled

		await blocker.close_all()
		assert cdp.detached is True
		assert not blocker._sessions
		assert not blocker._media_enabled

	asyncio.run(_run())


def test_resolve_locator_text_parallel_preserves_role_priority_and_frame_order():
	async def _run():
		class DelayedLocator:
			def __init__(self, count: int = 0, visible: bool = True, delay: float = 0.05):
				self._count = count
				self._visible = visible
				self._delay = delay
				self.first = self

			async def count(self) -> int:
				await asyncio.sleep(self._delay)
				return self._count

			async def is_visible(self) -> bool:
				return self._visible

		class Scope:
			def __init__(self, roles=None, text_loc=None):
				self._roles = roles or {}
				self._text_loc = text_loc or DelayedLocator(count=0)
				self.role_calls = 0

			def get_by_role(self, role: str, name: str = ""):
				self.role_calls += 1
				return self._roles.get(role, DelayedLocator(count=0))

			def get_by_text(self, text: str):
				return self._text_loc

		link_loc = DelayedLocator(count=1)
		text_loc = DelayedLocator(count=1)
		iframe_loc = DelayedLocator(count=1)
		iframe = Scope(roles={"button": iframe_loc})
		page = Scope(roles={"link": link_loc}, text_loc=text_loc)
		page.main_frame = page
		page.frames = [page, iframe]

		t0 = time.perf_counter()
		loc, _, _, matched = await sb._resolve_locator(page, text="Continue")
		elapsed = time.perf_counter() - t0
		assert matched is True
		assert loc is link_loc
		assert elapsed < 0.18, f"Expected parallel role lookup (<0.18s), got {elapsed:.3f}s"
		assert iframe.role_calls == 0

		page_empty = Scope()
		iframe1 = Scope(roles={"menuitem": iframe_loc})
		iframe2 = Scope(roles={"button": DelayedLocator(count=1)})
		page_empty.main_frame = page_empty
		page_empty.frames = [page_empty, iframe1, iframe2]
		t1 = time.perf_counter()
		loc2, _, _, matched2 = await sb._resolve_locator(page_empty, text="Continue")
		elapsed2 = time.perf_counter() - t1
		assert matched2 is True
		assert loc2 is iframe_loc
		assert elapsed2 < 0.25, f"Expected parallel secondary frame lookup (<0.25s), got {elapsed2:.3f}s"

	asyncio.run(_run())


def test_wait_for_text_uses_mutation_observer_and_retries_on_navigation(monkeypatch):
	async def _run():
		seen_scripts: list[str] = []
		calls = 0

		async def fake_eval(script, arg):
			nonlocal calls
			calls += 1
			seen_scripts.append(script)
			assert arg == "Order Confirmed"
			if calls == 1:
				raise RuntimeError("Page.evaluate: Execution context was destroyed")
			return True

		page = MagicMock()
		page.url = "https://shop.example/done"
		page.is_closed.return_value = False
		page.evaluate = AsyncMock(side_effect=fake_eval)
		monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page))

		res = await sb.wait_for(text="Order Confirmed", timeout_ms=2000)
		assert "Wait condition met (text='Order Confirmed')" in res
		assert calls == 2
		assert "MutationObserver" in seen_scripts[0]
		assert "textContent" in seen_scripts[0]

	asyncio.run(_run())


def test_force_kill_skips_sleep_when_idle_and_restart_reopens_tabs_in_parallel(monkeypatch):
	sleeps: list[float] = []
	cmds: list[list[str]] = []

	def fake_run_miss(cmd, **kwargs):
		cmds.append(cmd)
		res = MagicMock()
		res.returncode = 1
		return res

	monkeypatch.setattr(sb.subprocess, "run", fake_run_miss)
	monkeypatch.setattr(sb.time, "sleep", lambda s: sleeps.append(s))
	sb._force_kill_browser_processes()
	assert sleeps == []
	assert len(cmds) == 2

	cmds.clear()
	sleeps.clear()

	def fake_run_hit(cmd, **kwargs):
		cmds.append(cmd)
		res = MagicMock()
		res.returncode = 0
		return res

	monkeypatch.setattr(sb.subprocess, "run", fake_run_hit)
	sb._force_kill_browser_processes()
	assert sleeps == [0.3]
	assert len(cmds) == 4

	async def _run_restart():
		reopened_order: list[str] = []

		class ReopenPage:
			def __init__(self, url="about:blank"):
				self.url = url

			def is_closed(self):
				return False

			async def goto(self, target_url, **kwargs):
				await asyncio.sleep(0.08)
				self.url = target_url
				reopened_order.append(target_url)

			async def title(self):
				return "Title"

		initial_pages = [
			ReopenPage("https://a.example"),
			ReopenPage("https://b.example"),
			ReopenPage("https://c.example"),
			ReopenPage("https://d.example"),
		]
		created_pages: list[ReopenPage] = []

		class FakeCtx:
			async def new_page(self):
				p = ReopenPage()
				created_pages.append(p)
				return p

		mgr = sb.BrowserSessionManager("http://127.0.0.1:9222")
		mgr.get_pages = AsyncMock(return_value=initial_pages)
		mgr.get_active_page = AsyncMock(return_value=ReopenPage())
		mgr.get_context = AsyncMock(return_value=FakeCtx())
		monkeypatch.setattr(sb, "session", mgr)
		monkeypatch.setattr(sb, "close_browser", AsyncMock(return_value="closed"))
		monkeypatch.setattr(sb, "_save_persisted_headless", lambda _: None)
		monkeypatch.setattr(sb, "_apply_animations_to_page", AsyncMock())
		monkeypatch.setattr(sb.network_blocker, "ensure_page", AsyncMock())

		t0 = time.perf_counter()
		res = await sb._restart_browser_inner()
		elapsed = time.perf_counter() - t0
		assert "reopened 4 tab(s)" in res
		assert len(created_pages) == 3
		assert [p.url for p in created_pages] == ["https://b.example", "https://c.example", "https://d.example"]
		assert elapsed < 0.65, f"Expected parallel extra tab reopen (<0.65s incl 0.3s+0.08s), got {elapsed:.3f}s"

	asyncio.run(_run_restart())


def test_new_tab_avoids_duplicate_get_pages_and_connect_cdp_parallelizes(monkeypatch):
	async def _run():
		page = MagicMock()
		page.is_closed.return_value = False
		mgr = sb.BrowserSessionManager("http://127.0.0.1:9222")
		mgr._active_page = page
		mgr.get_pages = AsyncMock(return_value=[page])
		monkeypatch.setattr(sb, "session", mgr)
		monkeypatch.setattr(sb, "navigate", AsyncMock(return_value="Navigated tab [0] to 'https://x.test' [HTTP 200]"))

		out = await sb.new_tab("https://x.test")
		assert "(tab_id=t1)" in out
		mgr.get_pages.assert_not_awaited()

		mgr2 = sb.BrowserSessionManager("http://127.0.0.1:9222")
		fake_ctx = MagicMock()
		fake_browser = MagicMock()
		fake_browser.contexts = [fake_ctx]
		mgr2._playwright = MagicMock()
		mgr2._playwright.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)

		import threading

		detect_started = threading.Event()
		interceptor_started = threading.Event()
		overlap_seen = False

		def slow_detect():
			nonlocal overlap_seen
			detect_started.set()
			overlap_seen = interceptor_started.wait(timeout=2.0)
			return False

		async def slow_interceptor(ctx):
			interceptor_started.set()
			await asyncio.to_thread(detect_started.wait, 2.0)

		mgr2._detect_cdp_headless = slow_detect
		mgr2._ensure_interceptor = slow_interceptor
		ctx = await mgr2._connect_cdp()
		assert ctx is fake_ctx
		assert mgr2.headless is False
		assert overlap_seen, "Expected _detect_cdp_headless and _ensure_interceptor to run concurrently"

	asyncio.run(_run())


def test_telemetry_tracker_defers_header_extraction_until_get_network():
	tracker = sb.TelemetryTracker(maxlen=20)
	page = MagicMock()
	req = MagicMock()
	req.method = "GET"
	req.resource_type = "fetch"
	req.url = "https://bank.example/statement.pdf"
	req.timing = {"responseEnd": 42.4}
	resp = MagicMock()
	resp.request = req
	resp.status = 200
	resp.url = req.url
	resp.headers = {"Content-Type": "application/pdf", "Content-Length": "4096"}

	tracker._on_response(page, resp)
	raw_entry = tracker._network[page][0]
	assert "_raw_headers" in raw_entry
	assert raw_entry["content_type"] == ""
	assert raw_entry["size_bytes"] is None

	items, total = tracker.get_network(page, content_type="pdf")
	assert total == 1
	assert len(items) == 1
	assert items[0]["content_type"] == "application/pdf"
	assert items[0]["size_bytes"] == 4096
	assert "_raw_headers" not in items[0]

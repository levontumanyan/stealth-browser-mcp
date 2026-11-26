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
		assert "[0 | t1] Alpha - https://a.example" in lines[0]
		assert "[1 | t2] (Unresponsive / Busy) - https://b.example" in lines[1]
		assert "[2 | t3] Gamma - https://c.example (ACTIVE in this session)" in lines[2]
		assert "[3 | t4] Delta - https://d.example" in lines[3]

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
		loc.click.assert_awaited_once_with(timeout=5000)

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

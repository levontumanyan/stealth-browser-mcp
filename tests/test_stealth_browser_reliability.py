import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_stealth_browser_module():
	script_path = Path(__file__).resolve().parent.parent / "bin" / "stealth-browser"
	loader = importlib.machinery.SourceFileLoader("stealth_browser_rel_mod", str(script_path))
	spec = importlib.util.spec_from_loader(loader.name, loader)
	mod = importlib.util.module_from_spec(spec)
	loader.exec_module(mod)
	return mod


sb = _load_stealth_browser_module()


class _NavPage:
	def __init__(self, url: str = "https://bank.example/app"):
		self.url = url
		self.frames = [self]
		self.main_frame = self
		self.evaluate = AsyncMock()
		self.wait_for_load_state = AsyncMock()
		self._locators = {}

	def locator(self, selector: str):
		return self._locators[selector]


class _OverlayLocator:
	def __init__(self, count: int = 1):
		self._count = count
		self.click = AsyncMock()
		self.evaluate = AsyncMock(return_value=None)
		self.fill = AsyncMock()

	@property
	def first(self):
		return self

	async def count(self):
		return self._count


def test_nav_retry_snapshot_extract_state_and_get_content(monkeypatch):
	monkeypatch.setattr(sb.asyncio, "sleep", AsyncMock())
	nav_err = RuntimeError("Page.evaluate: Execution context was destroyed, most likely because of a navigation.")

	page = _NavPage()
	page.evaluate = AsyncMock(
		side_effect=[
			nav_err,
			{"totalMatched": 1, "items": [{"index": 0, "ref": "e0", "tag": "button", "selector": "#go", "text": "Go"}]},
		]
	)
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page))
	out_snap = asyncio.run(sb.snapshot())
	assert "[e0] <button> #go \"Go\"" in out_snap
	assert page.evaluate.await_count == 2
	page.wait_for_load_state.assert_awaited_once()

	page2 = _NavPage()
	page2.evaluate = AsyncMock(
		side_effect=[
			nav_err,
			{"url": page2.url, "title": "Order", "total_matched": 1, "returned": 1, "rows": [{"_index": 0, "price": "10.50"}]},
		]
	)
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page2))
	out_ext = asyncio.run(sb.extract("#form", fields={"price": "@value #price"}))
	assert "Extracted 1 row(s)" in out_ext
	assert page2.evaluate.await_count == 2

	page3 = _NavPage()
	page3.evaluate = AsyncMock(
		side_effect=[
			nav_err,
			[{"source": "Next.js (__NEXT_DATA__)", "keys": ["page"], "data": {"page": "/"}}],
		]
	)
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page3))
	out_state = asyncio.run(sb.state())
	assert "Next.js (__NEXT_DATA__)" in out_state
	assert page3.evaluate.await_count == 2
	assert page3.evaluate.call_args.kwargs.get("isolated_context") is False

	page4 = _NavPage()
	page4.evaluate = AsyncMock(side_effect=[None, "Loaded after redirect"])
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page4))
	out_body = asyncio.run(sb.get_content(selector="body", clean=True))
	assert out_body == "Loaded after redirect"
	assert page4.evaluate.await_count == 2

	page5 = _NavPage()
	raw_loc = MagicMock()
	raw_loc.first = raw_loc
	raw_loc.count = AsyncMock(side_effect=[0, 1])
	raw_loc.inner_text = AsyncMock(return_value="Raw body ready")
	page5._locators["body"] = raw_loc
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page5))
	out_raw = asyncio.run(sb.get_content(selector="body", clean=False))
	assert out_raw == "Raw body ready"
	assert raw_loc.count.await_count == 2

	page6 = _NavPage()
	page6.evaluate = AsyncMock(side_effect=nav_err)
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page6))
	out_eval = asyncio.run(sb.evaluate_js("1 + 1"))
	assert "JavaScript execution error:" in out_eval
	assert page6.evaluate.await_count == 1


def test_click_and_batch_overlay_fallback_and_two_phase_timeout(monkeypatch):
	overlay_err = RuntimeError("Locator.click: Timeout 2000ms exceeded.\nCall log:\n  - <div class=\"chat\"> intercepts pointer events")

	loc_sel = _OverlayLocator(count=1)
	loc_sel.click = AsyncMock(side_effect=overlay_err)
	page = _NavPage()
	page._locators["#pending"] = loc_sel
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page))

	res_sel = asyncio.run(sb.click(selector="#pending", timeout_ms=5000))
	assert "(js-dispatched after overlay interception)" in res_sel
	loc_sel.click.assert_awaited_once_with(timeout=2000)
	loc_sel.evaluate.assert_awaited_once()

	loc_ref = _OverlayLocator(count=1)
	loc_ref.click = AsyncMock(side_effect=overlay_err)
	loc_ref.evaluate = AsyncMock(side_effect=RuntimeError("detached"))
	page_ref = _NavPage()
	page_ref._locators['[data-sb-ref="e23"]'] = loc_ref
	page_ref.evaluate = AsyncMock(return_value=True)
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page_ref))

	res_ref = asyncio.run(sb.click(ref="e23", force=False, timeout_ms=5000))
	assert "(js-dispatched after overlay interception)" in res_ref
	loc_ref.click.assert_awaited_once_with(timeout=2000)
	page_ref.evaluate.assert_awaited_once()

	loc_slow = _OverlayLocator(count=1)
	loc_slow.click = AsyncMock(side_effect=[RuntimeError("Timeout 2000ms exceeded waiting for locator"), None])
	page_slow = _NavPage()
	page_slow._locators["#slow"] = loc_slow
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page_slow))

	res_slow = asyncio.run(sb.click(selector="#slow", timeout_ms=5000))
	assert "Clicked element matching '#slow'" in res_slow
	assert "(js-dispatched" not in res_slow
	assert [c.kwargs["timeout"] for c in loc_slow.click.call_args_list] == [2000, 3000]

	loc_batch = _OverlayLocator(count=1)
	loc_batch.click = AsyncMock(side_effect=overlay_err)
	page_batch = _NavPage()
	page_batch._locators["#submit"] = loc_batch
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page_batch))

	res_batch = asyncio.run(sb.batch(actions=[{"action": "click", "selector": "#submit", "timeout_ms": 5000}]))
	assert "[0] click '#submit' (js-dispatched after overlay interception): OK" in res_batch


def test_stale_tab_error_includes_open_tabs_and_batch_partial_progress(monkeypatch):
	class _TabPage:
		def __init__(self, url: str):
			self.url = url

		def is_closed(self):
			return False

		def on(self, *_a, **_kw):
			pass

	p1 = _TabPage("https://alpha.example/one")
	p2 = _TabPage("https://beta.example/two")
	mgr = sb.BrowserSessionManager("http://127.0.0.1:9999")
	mgr._context = MagicMock(pages=[p1, p2])
	mgr._browser = MagicMock(is_connected=lambda: True)
	t1 = mgr.get_tab_id(p1)
	t2 = mgr.get_tab_id(p2)

	with pytest.raises(ValueError) as exc_info:
		asyncio.run(mgr.resolve_page("t99"))
	assert f"[{t1}] https://alpha.example/one" in str(exc_info.value)
	assert f"[{t2}] https://beta.example/two" in str(exc_info.value)

	monkeypatch.setattr(mgr, "get_pages", AsyncMock(return_value=[p1, p2]))
	with pytest.raises(ValueError) as exc_idx:
		asyncio.run(mgr.resolve_page(5))
	assert f"[{t1}] https://alpha.example/one" in str(exc_idx.value)

	loc_ok = _OverlayLocator(count=1)
	loc_fail = _OverlayLocator(count=1)
	loc_fail.click = AsyncMock(side_effect=RuntimeError("boom"))
	page_b = _NavPage()
	page_b._locators["#ok"] = loc_ok
	page_b._locators["#fail"] = loc_fail
	monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=page_b))

	res = asyncio.run(
		sb.batch(
			actions=[
				{"action": "click", "selector": "#ok"},
				{"action": "click", "selector": "#fail", "timeout_ms": 1000},
				{"action": "click", "selector": "#never"},
			]
		)
	)
	assert "Batch failed at step [1] (1/3 completed)" in res
	assert "[0] click '#ok': OK" in res


def test_wait_for_human_preserves_tab_ids_across_headed_restart(monkeypatch):
	class _StubPage:
		def __init__(self, url: str):
			self.url = url
			self._closed = False

		def is_closed(self):
			return self._closed

		def on(self, *_a, **_kw):
			pass

	old_main = _StubPage("https://portal.example/login")
	old_other = _StubPage("https://docs.example/ref")
	new_main = _StubPage("https://portal.example/login")
	new_other = _StubPage("https://docs.example/ref")

	orig_state = (
		sb.session.headless,
		dict(sb.session._page_to_id),
		dict(sb.session._id_to_page),
		sb.session._next_tab_seq,
		dict(sb.session._active_pages),
	)
	try:
		sb.session.headless = True
		sb.session._page_to_id.clear()
		sb.session._id_to_page.clear()
		sb.session._active_pages.clear()
		sb.session._next_tab_seq = 1

		tid_main = sb.session.get_tab_id(old_main)
		tid_other = sb.session.get_tab_id(old_other)
		assert (tid_main, tid_other) == ("t1", "t2")

		restarted = {"done": False}

		async def _fake_get_pages():
			if not restarted["done"]:
				return [old_main, old_other]
			return [new_other, new_main]

		async def _fake_restart(headless=None, **_kw):
			restarted["done"] = True
			sb.session.headless = False
			sb.session._page_to_id.clear()
			sb.session._id_to_page.clear()
			sb.session._next_tab_seq = 10
			return "Restarted"

		monkeypatch.setattr(sb.session, "resolve_page", AsyncMock(return_value=old_main))
		monkeypatch.setattr(sb.session, "_detect_cdp_headless", lambda: True)
		monkeypatch.setattr(sb.session, "get_pages", _fake_get_pages)
		monkeypatch.setattr(sb, "restart_browser", _fake_restart)
		monkeypatch.setattr(sb, "_notify_human", lambda _reason: None)

		res = asyncio.run(
			sb.wait_for_human(
				reason="Login",
				url_contains="portal.example/login",
				timeout_s=5,
				poll_interval_s=0.1,
				notify=False,
			)
		)
		assert "Human handoff complete" in res
		assert sb.session.get_tab_id(new_main) == "t1"
		assert sb.session.get_tab_id(new_other) == "t2"
	finally:
		sb.session.headless, sb.session._page_to_id, sb.session._id_to_page, sb.session._next_tab_seq, sb.session._active_pages = (
			orig_state[0],
			orig_state[1],
			orig_state[2],
			orig_state[3],
			orig_state[4],
		)


def test_context_page_hook_auto_attaches_network_blocker(monkeypatch):
	handlers = {}

	class _FakeContext:
		def on(self, event, fn):
			handlers[event] = fn

	class _FakePopup:
		def is_closed(self):
			return False

		def on(self, *_a, **_kw):
			pass

	mgr = sb.BrowserSessionManager("http://127.0.0.1:9999")
	monkeypatch.setattr(mgr, "_configure_native_downloads", AsyncMock())
	monkeypatch.setattr(mgr, "_ensure_animation_init_script", AsyncMock())
	ensure_mock = AsyncMock(return_value=True)
	monkeypatch.setattr(sb.network_blocker, "ensure_page", ensure_mock)
	monkeypatch.setattr(sb.network_blocker, "_block_trackers_override", True)

	ctx = _FakeContext()

	async def _run():
		await mgr._ensure_interceptor(ctx)
		assert "page" in handlers
		popup = _FakePopup()
		handlers["page"](popup)
		await asyncio.sleep(0)
		return popup

	popup = asyncio.run(_run())
	ensure_mock.assert_awaited_once_with(popup)

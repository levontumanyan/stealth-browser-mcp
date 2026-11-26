import asyncio
import gc
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _load_stealth_browser():
	script_path = Path(__file__).resolve().parent.parent / "bin" / "stealth-browser"
	loader = SourceFileLoader("stealth_browser_multiclient_test", str(script_path))
	spec = spec_from_loader("stealth_browser_multiclient_test", loader)
	module = module_from_spec(spec)
	loader.exec_module(module)
	return module


sb = _load_stealth_browser()


class FakeResponse:
	def __init__(self, status: int = 200):
		self.status = status


class FakePage:
	def __init__(self, url: str = "about:blank", title: str = "Untitled"):
		self.url = url
		self._title = title
		self._closed = False
		self._listeners: dict[str, list] = {}
		self.brought_to_front = 0

	def is_closed(self) -> bool:
		return self._closed

	def on(self, event: str, handler):
		self._listeners.setdefault(event, []).append(handler)

	async def title(self) -> str:
		return self._title

	async def goto(self, url: str, timeout: int = 15000, wait_until: str = "domcontentloaded"):
		self.url = url
		self._title = f"Title:{url}"
		return FakeResponse(200)

	async def bring_to_front(self):
		self.brought_to_front += 1

	async def evaluate(self, script, arg=None, isolated_context: bool = True):
		return f"eval:{self.url}"

	async def close(self):
		self._closed = True
		for handler in list(self._listeners.get("close", [])):
			handler(self)


class FakeContext:
	def __init__(self, pages: list[FakePage]):
		self.pages = list(pages)
		self._closed = False

	def is_closed(self) -> bool:
		return self._closed

	async def new_page(self) -> FakePage:
		page = FakePage("about:blank", "New Tab")
		self.pages.append(page)
		return page

	async def close(self):
		self._closed = True


class FakeBrowser:
	def __init__(self, connected: bool = True):
		self._connected = connected

	def is_connected(self) -> bool:
		return self._connected

	async def close(self):
		self._connected = False


class FakeClientSession:
	pass


def _make_http_ctx(session_id: str):
	return SimpleNamespace(
		request_context=SimpleNamespace(
			request=SimpleNamespace(headers={"mcp-session-id": session_id}),
		),
		client_id=None,
		session=None,
	)


@pytest.fixture
def isolated_multiclient(monkeypatch):
	def _setup(pages: list[FakePage], headless: bool = True):
		mgr = sb.BrowserSessionManager("http://127.0.0.1:9999")
		mgr.headless = headless
		mgr._playwright = object()
		mgr._browser = FakeBrowser(connected=True)
		mgr._context = FakeContext(pages)
		monkeypatch.setattr(sb, "session", mgr)
		monkeypatch.setattr(sb, "keep_alive_mgr", sb.KeepAliveManager())
		monkeypatch.setattr(sb, "_attach_page_trackers", lambda p: None)
		monkeypatch.setattr(sb, "_apply_animations_to_page", AsyncMock(return_value=None))
		monkeypatch.setattr(mgr, "_detect_cdp_headless", lambda: mgr.headless)
		monkeypatch.setattr(sb, "_save_persisted_headless", lambda val: None)
		monkeypatch.setattr(sb, "_force_kill_browser_processes", lambda: None)
		return mgr

	return _setup


def test_session_key_derivation_and_weakref_cleanup(isolated_multiclient):
	mgr = isolated_multiclient([FakePage("https://a.example", "A")])

	assert sb._session_key(None) == "default"
	assert sb._session_key(_make_http_ctx("abc-123")) == "http:abc-123"
	assert sb._session_key(SimpleNamespace(client_id="cli-1")) == "client:cli-1"

	client_obj = FakeClientSession()
	sess_ctx = SimpleNamespace(session=client_obj)
	key = sb._session_key(sess_ctx)
	assert key.startswith("session:")

	page = mgr._context.pages[0]
	mgr.set_active_page(page, client_key=key)
	assert mgr.get_active_page_for(key) is page

	del sess_ctx
	del client_obj
	gc.collect()
	assert key not in mgr._active_pages


def test_two_concurrent_clients_independent_active_tabs(isolated_multiclient):
	async def _run():
		p1 = FakePage("https://alpha.example", "Alpha")
		p2 = FakePage("https://beta.example", "Beta")
		mgr = isolated_multiclient([p1, p2])

		ctx_a = _make_http_ctx("client-a")
		ctx_b = _make_http_ctx("client-b")

		await asyncio.gather(
			sb.switch_tab(tab=0, ctx=ctx_a),
			sb.switch_tab(tab=1, ctx=ctx_b),
		)

		res_a, res_b = await asyncio.gather(
			sb.evaluate_js(expression="location.href", tab=-1, ctx=ctx_a),
			sb.evaluate_js(expression="location.href", tab=-1, ctx=ctx_b),
		)
		assert res_a == "Result: eval:https://alpha.example"
		assert res_b == "Result: eval:https://beta.example"

		st_a = await sb.status(ctx=ctx_a)
		st_b = await sb.status(ctx=ctx_b)
		assert "Alpha - https://alpha.example (ACTIVE in this session)" in st_a
		assert "Beta - https://beta.example (ACTIVE in this session)" not in st_a
		assert "Beta - https://beta.example (ACTIVE in this session)" in st_b
		assert "Alpha - https://alpha.example (ACTIVE in this session)" not in st_b

		new_res = await sb.new_tab(url="https://gamma.example", ctx=ctx_b)
		assert "https://gamma.example" in new_res
		assert await sb.evaluate_js(expression="location.href", tab=-1, ctx=ctx_a) == "Result: eval:https://alpha.example"
		assert await sb.evaluate_js(expression="location.href", tab=-1, ctx=ctx_b) == "Result: eval:https://gamma.example"

		t1 = mgr.get_tab_id(p1)
		assert await mgr.resolve_page(t1, client_key=sb._session_key(ctx_b)) is p1

		close_res = await sb.close_tab(tab=-1, ctx=ctx_a)
		assert "https://alpha.example" in close_res
		assert p1.is_closed()
		assert mgr.get_active_page_for(sb._session_key(ctx_b)).url == "https://gamma.example"

	asyncio.run(_run())


def test_concurrent_get_context_serialized_by_connect_lock(monkeypatch):
	async def _run():
		mgr = sb.BrowserSessionManager("http://127.0.0.1:9999")
		start_calls = 0
		connect_calls = 0
		fake_ctx = FakeContext([FakePage("https://one.example", "One")])

		class _FakePWStarter:
			async def start(self):
				nonlocal start_calls
				start_calls += 1
				await asyncio.sleep(0.03)
				return SimpleNamespace(stop=AsyncMock())

		async def _fake_connect_cdp():
			nonlocal connect_calls
			connect_calls += 1
			await asyncio.sleep(0.03)
			mgr._browser = FakeBrowser(connected=True)
			mgr._context = fake_ctx
			return fake_ctx

		monkeypatch.setattr(sb, "async_playwright", lambda: _FakePWStarter())
		monkeypatch.setattr(mgr, "_connect_cdp", _fake_connect_cdp)

		results = await asyncio.gather(*(mgr.get_context() for _ in range(5)))
		assert all(r is fake_ctx for r in results)
		assert start_calls == 1
		assert connect_calls == 1

		await mgr.close()
		assert mgr._context is None
		assert mgr._playwright is None

	asyncio.run(_run())


def test_lifecycle_lock_serializes_concurrent_restarts_and_wait_for_human(isolated_multiclient, monkeypatch):
	async def _run():
		p1 = FakePage("https://app.example/login", "Login")
		mgr = isolated_multiclient([p1], headless=True)

		in_flight = 0
		max_in_flight = 0

		async def _slow_restart_inner(headless=None, url=None, client_key="default"):
			nonlocal in_flight, max_in_flight
			in_flight += 1
			max_in_flight = max(max_in_flight, in_flight)
			await asyncio.sleep(0.04)
			if headless is not None:
				mgr.headless = bool(headless)
			in_flight -= 1
			return f"Browser restarted successfully in {'Headless' if mgr.headless else 'Headed'} mode on tab [0]"

		monkeypatch.setattr(sb, "_restart_browser_inner", _slow_restart_inner)

		ctx_a = _make_http_ctx("client-a")
		ctx_b = _make_http_ctx("client-b")

		r1, r2 = await asyncio.gather(
			sb.restart_browser(headless=False, force=True, ctx=ctx_a),
			sb.restart_browser(headless=True, force=True, ctx=ctx_b),
		)
		assert "Browser restarted successfully" in r1
		assert "Browser restarted successfully" in r2
		assert max_in_flight == 1

		mgr.headless = True
		p1.url = "https://app.example/done"
		res_wfh = await sb.wait_for_human(
			reason="2FA",
			url_contains="/done",
			timeout_s=1,
			poll_interval_s=0.01,
			notify=False,
			ctx=ctx_a,
		)
		assert "Human handoff completed" in res_wfh
		assert "(switched to headed mode)" in res_wfh
		assert mgr.headless is False

	asyncio.run(_run())


def test_sibling_session_refusal_guards_and_force_override(isolated_multiclient, monkeypatch):
	async def _run():
		p1 = FakePage("https://bank.example/account", "Bank")
		p2 = FakePage("https://docs.example/page", "Docs")
		mgr = isolated_multiclient([p1, p2], headless=True)

		ctx_a = _make_http_ctx("client-a")
		ctx_b = _make_http_ctx("client-b")

		await sb.switch_tab(tab=0, ctx=ctx_a)
		await sb.switch_tab(tab=1, ctx=ctx_b)

		refused_headless = await sb.set_headless(headless=False, ctx=ctx_b)
		assert "Refusing to restart the browser to switch mode" in refused_headless
		assert "https://bank.example/account" in refused_headless
		assert "http:client-a" in refused_headless

		refused_restart = await sb.restart_browser(headless=False, ctx=ctx_b)
		assert "Refusing to restart the browser" in refused_restart
		assert "https://bank.example/account" in refused_restart

		refused_close = await sb.close_browser(ctx=ctx_b)
		assert "Refusing to close the browser" in refused_close
		assert "https://bank.example/account" in refused_close

		monkeypatch.setattr(
			sb,
			"_restart_browser_inner",
			AsyncMock(return_value="Browser restarted successfully in Headed mode on tab [0]"),
		)
		ok_headless = await sb.set_headless(headless=False, force=True, ctx=ctx_b)
		assert "Browser restarted successfully" in ok_headless

		ok_restart = await sb.restart_browser(headless=False, force=True, ctx=ctx_b)
		assert "Browser restarted successfully" in ok_restart

		ok_close = await sb.close_browser(force=True, ctx=ctx_b)
		assert "Browser completely closed" in ok_close

	asyncio.run(_run())


def test_sibling_keep_alive_triggers_refusal_even_without_active_tab(isolated_multiclient):
	async def _run():
		p1 = FakePage("https://portal.example/session", "Portal")
		p2 = FakePage("https://other.example", "Other")
		mgr = isolated_multiclient([p1, p2], headless=False)

		ctx_a = _make_http_ctx("client-a")
		ctx_b = _make_http_ctx("client-b")

		await sb.keep_alive(enable=True, tab=0, min_seconds=60, max_seconds=120, ctx=ctx_a)
		mgr.clear_client(sb._session_key(ctx_a))

		refused = await sb.close_browser(ctx=ctx_b)
		assert "Refusing to close the browser" in refused
		assert "[KEEP-ALIVE ACTIVE]" in refused
		assert "https://portal.example/session" in refused

		await sb.keep_alive_mgr.stop_all()

	asyncio.run(_run())


def test_mcp_tool_schemas_hide_ctx_and_bind_context_kwarg():
	tools = asyncio.run(sb.mcp.list_tools())
	assert len(tools) >= 26
	for tool in tools:
		props = tool.inputSchema.get("properties", {})
		req = tool.inputSchema.get("required", [])
		assert "ctx" not in props, f"tool {tool.name} exposed ctx in inputSchema properties"
		assert "ctx" not in req, f"tool {tool.name} exposed ctx in inputSchema required"

	registered = sb.mcp._tool_manager._tools
	expected_ctx_tools = {
		"status", "keep_alive", "navigate", "new_tab", "wait_for_human",
		"set_media", "set_animations", "snapshot", "batch", "get_content",
		"extract", "click", "type", "press_key", "scroll", "screenshot",
		"fetch", "network_requests", "console_logs", "state", "evaluate_js",
		"wait_for", "switch_tab", "close_tab", "close_browser",
		"restart_browser", "set_headless",
	}
	for name in expected_ctx_tools:
		assert name in registered
		assert registered[name].context_kwarg == "ctx", f"tool {name} missing context_kwarg='ctx'"

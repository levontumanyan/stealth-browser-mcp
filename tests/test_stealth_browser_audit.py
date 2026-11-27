import collections
import datetime
import importlib.machinery
import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "bin" / "stealth-browser-audit"
_loader = importlib.machinery.SourceFileLoader("stealth_browser_audit", str(_SCRIPT_PATH))
_spec = importlib.util.spec_from_loader("stealth_browser_audit", _loader)
assert _spec is not None
sba = importlib.util.module_from_spec(_spec)
_loader.exec_module(sba)


def make_sess_state() -> dict:
	return {
		"calls": 0,
		"errors": 0,
		"schema_reads": 0,
		"chars": 0,
		"input_chars": 0,
		"schema_chars": 0,
		"overflows": 0,
		"shared_execute_outputs": 0,
		"domains": set(),
		"tools": collections.Counter(),
		"date": "",
		"prev_tool": None,
		"prev_errored": False,
		"prev_was_screenshot": False,
	}


def make_execute_item(call_id: str, nested: list[tuple[str, str, dict]], out_text: str, status: str = "completed", error: str | None = None) -> dict:
	return {
		"id": call_id,
		"name": "execute",
		"state": {
			"status": status,
			"input": {"code": 'return await tools["stealth-browser"].navigate({url: "https://example.com"});'},
			"content": [{"type": "text", "text": out_text}],
			"error": error,
			"metadata": {
				"toolCalls": [
					{"tool": f"stealth-browser.{tool}", "status": tc_status, "input": inp}
					for tool, tc_status, inp in nested
				]
			},
		},
	}


class TestClassifyOutcome:
	def test_wait_timed_out_classified(self):
		assert sba.classify_outcome("wait_for", "Wait timed out after 30000ms waiting for selector 'table'") == "wait_timeout"

	def test_wait_for_error_prefixes(self):
		assert sba.classify_outcome("wait_for", "error in wait_for: boom") == "tool_error"
		assert sba.classify_outcome("extract", "error extracting rows: boom") == "tool_error"
		assert sba.classify_outcome("downloads", "error listing downloads: boom") == "tool_error"
		assert sba.classify_outcome("network_requests", "error listing network requests: boom") == "tool_error"
		assert sba.classify_outcome("console_logs", "error listing console logs: boom") == "tool_error"
		assert sba.classify_outcome("wait_for_human", "error during human handoff: boom") == "tool_error"

	def test_wrapped_json_error_miss(self):
		wrapped = '{"result":"Error clicking element \'#x\': Locator.click: Timeout 10000ms exceeded."}'
		assert sba.classify_outcome("click", wrapped) is None
		assert sba.classify_outcome("click", sba.unwrap_result_json(wrapped)) == "click_error"


class TestHarnessValidationMisattribution:
	# Exact payload shape from issue #25 (session ses_df1bea730ffe2Lg6eDmbcRbHzC):
	# a malformed opencode execute call's schema error attached to the nearest browser tool call.
	EXECUTE_SCHEMA_ERROR = (
		"{'type': 'tool.execution', 'message': 'Invalid arguments for tool \"execute\":\n"
		"- code: Missing key\n\nArguments provided:\n{\n"
		"  \"command\": \"await tools[\\\"stealth-browser\\\"].new_tab(\\\"https://example.com\\\")\"\n}"
		"'}"
	)

	def test_execute_schema_error_skipped(self):
		assert sba.classify_outcome("wait_for_human", self.EXECUTE_SCHEMA_ERROR, explicit_error=True) is None
		assert sba.classify_outcome("wait_for_human", self.EXECUTE_SCHEMA_ERROR) is None

	def test_other_harness_tool_schema_error_skipped(self):
		for named in ("shell", "read", "execute", "session_opencode"):
			body = f'Invalid arguments for tool "{named}": - code: Missing key'
			assert sba.classify_outcome("wait_for_human", body, explicit_error=True) is None, named

	def test_browser_tool_schema_error_counted_as_validation(self):
		body = 'Invalid arguments for tool "stealth-browser_new_tab": - root: Expected object'
		assert sba.classify_outcome("new_tab", body, explicit_error=True) == "validation_error"
		body = 'Invalid arguments for tool "click": - selector: Missing key'
		assert sba.classify_outcome("click", body, explicit_error=True) == "validation_error"

	def test_escaped_quote_variant_matched(self):
		body = '[object Object] || "fail2:Error: Invalid arguments for tool \\"stealth-browser_type\\":\n- selector: Missing key"'
		assert sba.classify_outcome("type", body, explicit_error=True) == "validation_error"
		body = 'Invalid arguments for tool \\"execute\\": - code: Missing key'
		assert sba.classify_outcome("wait_for_human", body, explicit_error=True) is None

	def test_deeply_quoted_payload_not_matched(self):
		# Search is bounded to the head window: a body that only quotes an invalid-args
		# payload past the first 350 chars keeps its original classification.
		body = "Error clicking element '#x': " + "pad " * 100 + 'Invalid arguments for tool "execute": - code: Missing key'
		assert sba.harness_validation_tool(body) is None
		assert sba.classify_outcome("click", body, explicit_error=True) == "click_error"

	def test_nested_execute_schema_error_not_attributed_to_wait_for_human(self):
		c = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)
		sess = make_sess_state()
		item = make_execute_item(
			"c8",
			[("wait_for_human", "completed", {})],
			TestHarnessValidationMisattribution.EXECUTE_SCHEMA_ERROR,
			status="error",
			error=TestHarnessValidationMisattribution.EXECUTE_SCHEMA_ERROR,
		)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].wait_for_human({});', item, None, None, sess)
		assert sum(c.tool_errors["wait_for_human"].values()) == 0
		assert sess["errors"] == 0
		assert c.call_records[-1]["is_error"] is False


class TestParseValidationSignature:
	def test_wrapped_json_validation_error(self):
		wrapped = '{"result":"1 validation error for navigateArgs\\nurl\\n  Field required [type=missing]"}'
		assert sba.classify_outcome("navigate", sba.unwrap_result_json(wrapped)) == "validation_error"
		sig = sba.parse_validation_signature("navigate", sba.unwrap_result_json(wrapped), {"timeout_ms": 5000})
		assert "field 'url'" in sig
		assert "passed: timeout_ms" in sig


class TestSilentParamMismatch:
	def test_stale_tab_vs_index_rule_retired(self):
		assert sba.detect_silent_param_mismatch("close_tab", {"tab": "t4"}) is None
		assert sba.detect_silent_param_mismatch("switch_tab", {"tab": 0}) is None

	def test_tab_index_still_flagged(self):
		msg = sba.detect_silent_param_mismatch("close_tab", {"tab_index": 2})
		assert msg is not None and "tab_index" in msg


class TestKnownTools:

	def test_known_tools_covers_every_server_tool(self):
		# KNOWN_TOOLS is load-bearing for error visibility (classify_outcome skips harness
		# validation errors naming non-KNOWN tools), so drift must fail tests, not hide errors.
		server_src = (_SCRIPT_PATH.parent / "stealth-browser").read_text(encoding="utf-8")
		server_tools = set(re.findall(r"@mcp\.tool\(\)\s*(?:@[^\n]*\s*)*async def ([a-z0-9_]+)", server_src))
		assert server_tools, "no @mcp.tool() decorators found in bin/stealth-browser"
		assert server_tools - set(sba.KNOWN_TOOLS) == set(), (
			f"KNOWN_TOOLS is missing server tools: {sorted(server_tools - set(sba.KNOWN_TOOLS))}"
		)


class TestAgyOverflow:

	def test_expand_overflow_truncated_variant_with_trailing_prose(self, tmp_path, monkeypatch):
		monkeypatch.setattr(sba, "BRAIN_DIR", tmp_path)
		out_file = tmp_path / "steps" / "10" / "output.txt"
		out_file.parent.mkdir(parents=True)
		out_file.write_text("full content here", encoding="utf-8")
		stub = (
			f"The tool's output was truncated due to being too long. "
			f"You can find the full output in a file at file://{out_file}."
			"\n\nHere is a preview of the output:"
		)
		body, overflowed = sba.expand_agy_overflow(stub)
		assert overflowed is True
		assert body == "full content here"

	def test_overflow_path_outside_brain_dir_rejected(self, tmp_path, monkeypatch):
		monkeypatch.setattr(sba, "BRAIN_DIR", tmp_path / "brain")
		(tmp_path / "brain").mkdir()
		out_file = tmp_path / "outside" / "secret.txt"
		out_file.parent.mkdir(parents=True)
		out_file.write_text("should not be read", encoding="utf-8")
		stub = f"The output was large and was saved to: file://{out_file}"
		body, overflowed = sba.expand_agy_overflow(stub)
		assert overflowed is False
		assert body == stub


class TestNestedCharAttribution:
	def _collector(self) -> sba.AuditCollector:
		return sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)

	def test_single_nested_call_gets_full_output(self):
		c = self._collector()
		sess = make_sess_state()
		body = "R" * 1000
		item = make_execute_item("c1", [("navigate", "completed", {"url": "https://example.com"})], body)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].navigate({});', item, None, None, sess)
		assert sess["calls"] == 1
		assert sess["chars"] == len(body)
		assert c.call_chars["navigate"] == [len(body)]
		assert sess["shared_execute_outputs"] == 0

	def test_multi_nested_call_counts_execute_output_once(self):
		c = self._collector()
		sess = make_sess_state()
		body = "R" * 1001
		item = make_execute_item(
			"c2",
			[
				("navigate", "completed", {"url": "https://example.com"}),
				("get_content", "completed", {"selector": "body"}),
			],
			body,
		)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].navigate({});', item, None, None, sess)
		assert sess["calls"] == 2
		assert sess["chars"] == len(body)
		assert sum(c.call_chars["navigate"]) + sum(c.call_chars["get_content"]) == len(body)
		assert max(abs(x - len(body) // 2) for x in c.call_chars["navigate"] + c.call_chars["get_content"]) <= 1
		assert sess["shared_execute_outputs"] == 1


class TestNestedErrorAttribution:
	def _collector(self) -> sba.AuditCollector:
		return sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=4)

	def test_wrapped_error_attributed_once_to_last_call(self):
		c = self._collector()
		sess = make_sess_state()
		wrapped = '{"result":"Wait timed out after 30000ms waiting for selector \'table\'"}'
		item = make_execute_item(
			"c3",
			[
				("navigate", "completed", {"url": "https://example.com"}),
				("wait_for", "completed", {"selector": "table"}),
			],
			wrapped,
		)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].navigate({});', item, None, None, sess)
		assert c.error_counts["wait_timeout"] == 1
		assert c.tool_errors["wait_for"]["wait_timeout"] == 1
		assert sum(c.tool_errors["navigate"].values()) == 0
		assert sess["errors"] == 1

	def test_wrapped_click_error_single_call(self):
		c = self._collector()
		sess = make_sess_state()
		wrapped = '{"result":"Error clicking element \'#radix-_r_5_\': Locator.click: Timeout 10000ms exceeded."}'
		item = make_execute_item("c4", [("click", "completed", {"selector": "#radix-_r_5_"})], wrapped)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].navigate({});', item, None, None, sess)
		assert c.error_counts["click_error"] == 1

	def test_execute_level_error_not_spread_to_earlier_calls(self):
		c = self._collector()
		sess = make_sess_state()
		item = make_execute_item(
			"c5",
			[
				("navigate", "completed", {"url": "https://example.com"}),
				("snapshot", "completed", {}),
			],
			"ok output",
			status="error",
		)
		item["state"]["error"] = "execute timed out"
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].navigate({});', item, None, None, sess)
		assert sum(c.tool_errors["navigate"].values()) == 0
		assert sum(c.tool_errors["snapshot"].values()) == 1

	def test_earlier_errored_call_not_doubled_on_last(self):
		c = self._collector()
		sess = make_sess_state()
		item = make_execute_item(
			"c7",
			[
				("click", "error", {"selector": "#x"}),
				("get_content", "completed", {"selector": "body"}),
			],
			'{"result":"Content of body ..."}',
			error="execute failed: click step returned error",
		)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].click({});', item, None, None, sess)
		assert sess["errors"] == 1
		assert sum(c.tool_errors["click"].values()) == 1
		assert sum(c.tool_errors["get_content"].values()) == 0
		assert c.error_counts.get("other_error", 0) == 1

	def test_per_call_status_error_respected(self):
		c = self._collector()
		sess = make_sess_state()
		item = make_execute_item(
			"c6",
			[
				("navigate", "completed", {"url": "https://example.com"}),
				("click", "error", {"selector": "#x"}),
				("snapshot", "completed", {}),
			],
			"ok output",
		)
		c._record_nested_calls("s1", "t", "execute", 'await tools["stealth-browser"].navigate({});', item, None, None, sess)
		assert sum(c.tool_errors["click"].values()) == 1
		assert sum(c.tool_errors["navigate"].values()) == 0
		assert sum(c.tool_errors["snapshot"].values()) == 0


class TestComputeDiff:
	def _baseline(self, legacy: bool) -> dict:
		snapshot = {
			"generated_at": "2026-11-20T00:00:00+00:00",
			"filters": {"since": None},
			"summary": {
				"sessions": 2,
				"total_calls": 100,
				"schema_reads_per_session": 2.0,
				"schema_reads_in_browser_sessions": 4,
				"mcp_timeouts_3m": 0,
				"slow_non_timeout_calls": 0,
				"validation_errors_total": 0,
				"silent_param_mismatches_total": 0,
			},
			"tools": {},
			"evaluate_js": {"total": 0, "intents": {}},
		}
		if not legacy:
			snapshot["summary"]["schema_reads_per_agy_session"] = 4.0
			snapshot["summary"]["sessions_by_source"] = {"agy": 1, "opencode": 1}
		else:
			snapshot["summary"]["sessions_by_source"] = {"agy": 1, "opencode": 1}
		return snapshot


	def test_agy_rate_derived_from_legacy_baseline(self):
		legacy = self._baseline(legacy=True)
		del legacy["summary"]["schema_reads_per_session"]
		diff = sba.compute_diff(legacy, self._baseline(legacy=False))
		row = next(k for k in diff["kpis"] if k["metric"] == "Schema reads / AGY session")
		assert row["before"] == 4.0

	def test_schema_read_regression_flagged(self):
		current = self._baseline(legacy=False)
		current["summary"]["schema_reads_per_agy_session"] = 8.0
		diff = sba.compute_diff(self._baseline(legacy=False), current)
		row = next(k for k in diff["kpis"] if k["metric"] == "Schema reads / AGY session")
		assert row["delta"] == 4.0 and row["status"] == "regressed"

	def test_schema_read_improvement_flagged(self):
		current = self._baseline(legacy=False)
		current["summary"]["schema_reads_per_agy_session"] = 1.0
		diff = sba.compute_diff(self._baseline(legacy=False), current)
		row = next(k for k in diff["kpis"] if k["metric"] == "Schema reads / AGY session")
		assert row["delta"] == -3.0 and row["status"] == "improved"


class TestInputCharTracking:
	def test_record_call_tracks_input_chars(self):
		c = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)
		sess = make_sess_state()
		raw_args = {"expression": "document.title"}
		c._record_call(
			source="opencode",
			sid="s1",
			title="t",
			tool="evaluate_js",
			raw_args=raw_args,
			body="ok",
			explicit_error=False,
			duration_s=0.5,
			sess_state=sess,
			nested=True,
		)
		expected = len(json.dumps(raw_args))
		assert sess["input_chars"] == expected
		assert c.call_input_chars["evaluate_js"] == [expected]


@pytest.fixture()
def agy_env(tmp_path, monkeypatch):
	monkeypatch.setattr(sba, "BRAIN_DIR", tmp_path)
	monkeypatch.setattr(sba, "SUMMARY_DB", tmp_path / "missing.db")
	monkeypatch.setattr(sba, "CACHE_DIR", tmp_path / ".cache")
	return tmp_path


def _write_agy_session(root: Path, cid: str, compact_output: str, full_output: str, overflow_file: Path | None = None, schema_reads: int = 0) -> None:
	log_dir = root / cid / ".system_generated" / "logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	if overflow_file is not None:
		overflow_file.parent.mkdir(parents=True, exist_ok=True)
		overflow_file.write_text(full_output, encoding="utf-8")
		full_output = f"The output was large and was saved to: file://{overflow_file}."
	args = {"ServerName": "stealth-browser", "ToolName": "snapshot", "Arguments": {"max_elements": 80}}
	tool_calls = [
		{"name": "read_file", "args": {"Path": "/mcp/stealth-browser/navigate.json"}, "created_at": "2026-11-20T09:59:00Z"}
		for _ in range(schema_reads)
	]
	tool_calls.append({"name": "call_mcp_tool", "args": args, "created_at": "2026-11-20T10:00:00Z"})
	schema_generic = [
		json.dumps({"type": "GENERIC", "content": 'Created At: 2026-11-20T09:59:05Z\n{"name":"navigate"} schema body'})
		for _ in range(schema_reads)
	]
	full_lines = [
		json.dumps({"tool_calls": tool_calls}),
		*schema_generic,
		json.dumps({"type": "GENERIC", "content": f"Created At: 2026-11-20T10:00:05Z\nCompleted At: 2026-11-20T10:00:06Z\n{full_output}"}),
		json.dumps({"type": "PLANNER_RESPONSE", "content": "done"}),
	]
	(log_dir / "transcript_full.jsonl").write_text("\n".join(full_lines), encoding="utf-8")
	compact_lines = [
		json.dumps({"tool_calls": tool_calls}),
		*schema_generic,
		json.dumps({"type": "GENERIC", "content": f"Created At: 2026-11-20T10:00:05Z\nCompleted At: 2026-11-20T10:00:06Z\n{compact_output}"}),
		json.dumps({"type": "PLANNER_RESPONSE", "content": "done"}),
	]
	(log_dir / "transcript.jsonl").write_text("\n".join(compact_lines), encoding="utf-8")


class TestCollectAgy:
	def test_prefers_transcript_full_and_expands_overflow(self, agy_env):
		overflow = agy_env / "overflow.txt"
		full_output = "F" * 15591
		compact_output = "C" * 4000
		_write_agy_session(agy_env, "cid-full-1111", compact_output, full_output, overflow_file=overflow)

		collector = sba.AuditCollector(since=None, includes=["cid-full-1111"], excludes=[], slow_threshold=20.0, max_samples=2)
		collector.collect_agy()
		assert collector.tool_counts["snapshot"] == 1
		assert collector.sessions[0]["chars"] == len(full_output)
		assert collector.sessions[0]["overflows"] == 1
		assert collector.agy_overflow_expansions == 1
		assert collector.build_snapshot(None, "all")["summary"]["agy_output_overflows"] == 1

	def test_falls_back_to_compact_transcript(self, agy_env):
		compact_output = "C" * 4000
		full_output = "F" * 9000
		_write_agy_session(agy_env, "cid-compact-22", compact_output, full_output)
		(agy_env / "cid-compact-22" / ".system_generated" / "logs" / "transcript_full.jsonl").unlink()

		collector = sba.AuditCollector(since=None, includes=["cid-compact-22"], excludes=[], slow_threshold=20.0, max_samples=2)
		collector.collect_agy()
		assert collector.sessions[0]["chars"] == len(compact_output)
		assert collector.sessions[0]["overflows"] == 0

	def test_schema_read_chars_counted(self, agy_env):
		log_dir = agy_env / "cid-schema-33" / ".system_generated" / "logs"
		log_dir.mkdir(parents=True)
		schema_body = '{"name":"navigate","inputSchema":{}}' + "x" * 500
		navigate_out = "Navigated to https://example.com"
		lines = [
			json.dumps({"tool_calls": [
				{"name": "read_file", "args": {"Path": "/mcp/stealth-browser/navigate.json"}, "created_at": "2026-11-20T10:00:00Z"},
				{"name": "call_mcp_tool", "args": {"ServerName": "stealth-browser", "ToolName": "navigate", "Arguments": {"url": "https://example.com"}}, "created_at": "2026-11-20T10:00:01Z"},
			]}),
			json.dumps({"type": "GENERIC", "content": f"Created At: 2026-11-20T10:00:05Z\n{schema_body}"}),
			json.dumps({"type": "GENERIC", "content": f"Created At: 2026-11-20T10:00:06Z\n{navigate_out}"}),
			json.dumps({"type": "PLANNER_RESPONSE", "content": "done"}),
		]
		(log_dir / "transcript.jsonl").write_text("\n".join(lines), encoding="utf-8")

		collector = sba.AuditCollector(since=None, includes=["cid-schema-33"], excludes=[], slow_threshold=20.0, max_samples=2)
		collector.collect_agy()
		assert collector.sessions[0]["schema_reads"] == 1
		assert collector.sessions[0]["schema_chars"] == len(schema_body)
		assert collector.tool_counts["navigate"] == 1
		snap = collector.build_snapshot(None, "all")
		assert snap["summary"]["schema_read_chars"] == len(schema_body)


class TestSnapshotSummary:
	def test_schema_reads_per_agy_session_uses_agy_denominator(self, agy_env):
		_write_agy_session(agy_env, "cid-agy-4444", "out", "out", schema_reads=4)
		collector = sba.AuditCollector(since=None, includes=["cid-agy-4444"], excludes=[], slow_threshold=20.0, max_samples=2)
		collector.collect_agy()

		oc_sess = make_sess_state()
		oc_sess["calls"] = 1
		oc_sess["chars"] = 10
		oc_sess["tools"]["click"] = 1
		oc_sess["date"] = "2026-11-20"
		collector._finalize_session("opencode", "ses_oc_5555", "t", oc_sess, "")

		snap = collector.build_snapshot(None, "all")
		assert snap["summary"]["sessions"] == 2
		assert snap["summary"]["sessions_by_source"] == {"agy": 1, "opencode": 1}
		assert snap["summary"]["schema_reads_total"] == 4
		assert snap["summary"]["schema_reads_per_session"] == 2.0
		assert snap["summary"]["schema_reads_per_agy_session"] == 4.0

	def test_schema_reads_per_agy_session_excludes_opencode_reads(self, agy_env):
		_write_agy_session(agy_env, "cid-agy-6666", "out", "out", schema_reads=2)
		collector = sba.AuditCollector(since=None, includes=["cid-agy-6666"], excludes=[], slow_threshold=20.0, max_samples=2)
		collector.collect_agy()

		oc_sess = make_sess_state()
		oc_sess["calls"] = 1
		oc_sess["chars"] = 10
		oc_sess["schema_reads"] = 3
		oc_sess["schema_chars"] = 300
		oc_sess["tools"]["click"] = 1
		oc_sess["date"] = "2026-11-20"
		collector._finalize_session("opencode", "ses_oc_7777", "t", oc_sess, "")

		snap = collector.build_snapshot(None, "all")
		assert snap["summary"]["schema_reads_total"] == 5
		assert snap["summary"]["schema_reads_per_session"] == 2.5
		assert snap["summary"]["schema_reads_per_agy_session"] == 2.0


class TestStripAgyHeaders:
	def test_peels_headers_and_preserves_body(self):
		raw = "Created At: 2026-11-20T10:00:05Z\nCompleted At: 2026-11-20T10:00:08Z\nline1\nCreated At: body-not-header\n"
		body, created, completed = sba.strip_agy_headers(raw)
		assert body == "line1\nCreated At: body-not-header"
		assert created == datetime.datetime(2026, 11, 20, 10, 0, 5, tzinfo=datetime.timezone.utc)
		assert completed == datetime.datetime(2026, 11, 20, 10, 0, 8, tzinfo=datetime.timezone.utc)


class TestLoadAgyTitles:
	def test_prefers_conversation_summaries_over_fts(self, tmp_path, monkeypatch):
		db = tmp_path / "summaries.db"
		conn = sqlite3.connect(db)
		conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT)")
		conn.execute("CREATE TABLE conversations_fts (conversation_id TEXT, title TEXT)")
		conn.execute("INSERT INTO conversation_summaries VALUES ('c1', 'Primary Title')")
		conn.execute("INSERT INTO conversations_fts VALUES ('c1', 'Stale FTS Title')")
		conn.execute("INSERT INTO conversations_fts VALUES ('c2', 'FTS Only')")
		conn.commit()
		conn.close()
		monkeypatch.setattr(sba, "SUMMARY_DB", db)
		titles = sba.load_agy_titles()
		assert titles == {"c1": "Primary Title", "c2": "FTS Only"}


class TestAgyFastPaths:
	def test_selective_line_parsing_and_auth_re(self, agy_env):
		log_dir = agy_env / "cid-fast-1" / ".system_generated" / "logs"
		log_dir.mkdir(parents=True)
		lines = [
			"not valid json at all",
			json.dumps({"type": "USER", "content": "Please help with 2FA login"}),
			json.dumps({"tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}]}),
			json.dumps({"type": "GENERIC", "content": "Created At: 2026-11-20T09:58:00Z\nfile1\nfile2"}),
			json.dumps({"type": "PLANNER_RESPONSE", "content": "ok"}),
			json.dumps({"tool_calls": [
				{"name": "run_command", "args": {"CommandLine": "echo hi"}},
				{"name": "call_mcp_tool", "args": {"ServerName": "stealth-browser", "ToolName": "navigate", "Arguments": {"url": "https://example.com"}}},
			]}),
			json.dumps({"type": "GENERIC", "content": "Created At: 2026-11-20T10:00:00Z\nignored"}),
			json.dumps({"type": "GENERIC", "content": "Created At: 2026-11-20T10:00:01Z\nCompleted At: 2026-11-20T10:00:03Z\nNavigated"}),
			json.dumps({"type": "PLANNER_RESPONSE", "content": "done"}),
		]
		(log_dir / "transcript.jsonl").write_text("\n".join(lines), encoding="utf-8")

		zero_dir = agy_env / "cid-fast-0" / ".system_generated" / "logs"
		zero_dir.mkdir(parents=True)
		(zero_dir / "transcript.jsonl").write_text(
			json.dumps({"type": "USER", "content": "stealth-browser login captcha"}) + "\n",
			encoding="utf-8",
		)

		collector = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)
		collector.collect_agy()
		assert len(collector.sessions) == 1
		assert collector.sessions[0]["sid"] == "cid-fast-1"
		assert collector.tool_counts["navigate"] == 1
		assert collector.auth_sessions == 1
		assert collector.call_latencies["navigate"] == [2.0]

	def test_parallel_path_deterministic_over_16_sessions(self, agy_env):
		for idx in range(18):
			_write_agy_session(agy_env, f"cid-par-{idx:02d}", "out", f"full-{idx}")
		c1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)
		c1.collect_agy()
		c2 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)
		c2.collect_agy()
		assert len(c1.sessions) == 18
		assert [s["sid"] for s in c1.sessions] == [s["sid"] for s in c2.sessions]
		assert [r["session_id"] for r in c1.call_records] == [r["session_id"] for r in c2.call_records]


class TestOpencodeSqlPushdown:
	def _make_db(self, db_path: Path) -> None:
		conn = sqlite3.connect(db_path)
		conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT)")
		conn.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
		conn.execute("CREATE INDEX idx_sm_sess ON session_message(session_id)")
		conn.execute("CREATE INDEX idx_sm_time ON session_message(time_created)")
		conn.executemany(
			"INSERT INTO session_v2 VALUES (?, ?)",
			[("ses_aaa1", "Alpha"), ("ses_bbb2", "Beta"), ("ses_old3", "Old")],
		)
		now_ms = int(datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
		old_ms = int(datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)

		def msg_payload(call_id: str, ts_ms: int) -> str:
			return json.dumps({
				"content": [
					{"type": "tool_use", "id": f"{call_id}-other", "name": "grep", "input": {"pattern": "x"}},
					{
						"type": "tool_use",
						"id": call_id,
						"name": "stealth-browser_navigate",
						"input": {"url": "https://example.com"},
						"state": {"status": "completed", "output": "Navigated", "time": {"start": ts_ms, "end": ts_ms + 1500}},
					},
				]
			})

		conn.executemany(
			"INSERT INTO session_message VALUES (?, ?, ?, ?)",
			[
				("m1", "ses_aaa1", now_ms, msg_payload("c1", now_ms)),
				("m2", "ses_bbb2", now_ms, msg_payload("c2", now_ms)),
				("m3", "ses_old3", old_ms, msg_payload("c3", old_ms)),
			],
		)
		conn.commit()
		conn.close()

	def test_includes_and_since_sql_pushdown(self, tmp_path, monkeypatch):
		self._make_db(tmp_path / "opencode.db")
		monkeypatch.setattr(sba, "OPENCODE_DIR", tmp_path)

		c_inc = sba.AuditCollector(since=None, includes=["ses_aaa"], excludes=[], slow_threshold=20.0, max_samples=2)
		c_inc.collect_opencode()
		assert [s["sid"] for s in c_inc.sessions] == ["ses_aaa1"]
		assert c_inc.tool_counts["navigate"] == 1

		since = datetime.datetime(2026, 11, 1, 0, 0, tzinfo=datetime.timezone.utc)
		c_since = sba.AuditCollector(since=since, includes=[], excludes=[], slow_threshold=20.0, max_samples=2)
		c_since.collect_opencode()
		assert sorted(s["sid"] for s in c_since.sessions) == ["ses_aaa1", "ses_bbb2"]
		assert c_since.tool_counts["navigate"] == 2


class TestSessionCache:
	def test_agy_cache_hit_and_invalidation_on_mtime_size(self, agy_env, monkeypatch):
		_write_agy_session(agy_env, "cid-cache-1", "compact", "F" * 100)
		cache_dir = agy_env / ".cache"

		c1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c1.collect_agy()
		assert c1.sessions[0]["chars"] == 100
		assert (cache_dir / f"cache-v{sba.CACHE_SCHEMA_VERSION}.sqlite").exists()

		called = []
		orig_parse = sba._parse_agy_file

		def spy_parse(*args, **kwargs):
			called.append(args[0])
			return orig_parse(*args, **kwargs)

		monkeypatch.setattr(sba, "_parse_agy_file", spy_parse)
		c2 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c2.collect_agy()
		assert called == []
		assert c2.sessions[0]["chars"] == 100
		assert c2.call_records == c1.call_records

		_write_agy_session(agy_env, "cid-cache-1", "compact", "G" * 250)
		c3 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c3.collect_agy()
		assert called == ["cid-cache-1"]
		assert c3.sessions[0]["chars"] == 250

	def test_opencode_cache_hit_and_time_updated_invalidation(self, tmp_path, monkeypatch):
		db_path = tmp_path / "opencode.db"
		conn = sqlite3.connect(db_path)
		conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, time_updated INTEGER)")
		conn.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
		ts1 = int(datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
		msg1 = json.dumps({
			"content": [{
				"type": "tool_use",
				"id": "call-1",
				"name": "stealth-browser_navigate",
				"input": {"url": "https://example.com"},
				"state": {"status": "completed", "output": "Navigated", "time": {"start": ts1, "end": ts1 + 1000}},
			}]
		})
		conn.execute("INSERT INTO session_v2 VALUES ('ses_c1', 'Cache Test', ?)", (ts1,))
		conn.execute("INSERT INTO session_message VALUES ('m1', 'ses_c1', ?, ?)", (ts1, msg1))
		conn.commit()
		conn.close()

		monkeypatch.setattr(sba, "OPENCODE_DIR", tmp_path)
		cache_dir = tmp_path / ".cache"

		c_uncached = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir, use_cache=False)
		c_uncached.collect_opencode()

		c1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c1.collect_opencode()
		c2 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c2.collect_opencode()

		s_unc = c_uncached.build_snapshot(None, "opencode")
		s_hit = c2.build_snapshot(None, "opencode")
		s_unc.pop("generated_at")
		s_hit.pop("generated_at")
		assert s_hit == s_unc
		assert c2.call_records == c_uncached.call_records

		ts2 = ts1 + 5000
		msg2 = json.dumps({
			"content": [{
				"type": "tool_use",
				"id": "call-2",
				"name": "stealth-browser_click",
				"input": {"selector": "#btn"},
				"state": {"status": "completed", "output": "Clicked", "time": {"start": ts2, "end": ts2 + 500}},
			}]
		})
		conn = sqlite3.connect(db_path)
		conn.execute("INSERT INTO session_message VALUES ('m2', 'ses_c1', ?, ?)", (ts2, msg2))
		conn.execute("UPDATE session_v2 SET time_updated = ? WHERE id = 'ses_c1'", (ts2,))
		conn.commit()
		conn.close()

		c3 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c3.collect_opencode()
		assert c3.tool_counts["navigate"] == 1
		assert c3.tool_counts["click"] == 1
		assert len(c3.call_records) == 2

	def test_schema_version_mismatch_invalidates_cache(self, agy_env, monkeypatch):
		_write_agy_session(agy_env, "cid-ver-1", "compact", "V" * 80)
		cache_dir = agy_env / ".cache"

		c1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c1.collect_agy()
		db = cache_dir / f"cache-v{sba.CACHE_SCHEMA_VERSION}.sqlite"
		conn = sqlite3.connect(db)
		conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
		conn.commit()
		conn.close()

		called = []
		orig_parse = sba._parse_agy_file

		def spy_parse(*args, **kwargs):
			called.append(args[0])
			return orig_parse(*args, **kwargs)

		monkeypatch.setattr(sba, "_parse_agy_file", spy_parse)
		c2 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c2.collect_agy()
		assert called == ["cid-ver-1"]
		assert c2.sessions[0]["chars"] == 80

	def test_no_cache_and_unwritable_cache_dir_fallback(self, agy_env):
		_write_agy_session(agy_env, "cid-fb-1", "compact", "U" * 60)
		no_cache_dir = agy_env / "never_created"
		c1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=no_cache_dir, use_cache=False)
		c1.collect_agy()
		assert not no_cache_dir.exists()
		assert c1.sessions[0]["chars"] == 60

		blocker = agy_env / "not_a_dir"
		blocker.write_text("file", encoding="utf-8")
		c2 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=blocker, use_cache=True)
		c2.collect_agy()
		assert c2.sessions[0]["chars"] == 60

	def test_orphan_row_pruning_on_unscoped_full_scans(self, agy_env, tmp_path, monkeypatch):
		import shutil

		_write_agy_session(agy_env, "cid-keep", "compact", "K" * 50)
		_write_agy_session(agy_env, "cid-gone", "compact", "G" * 50)
		cache_dir = agy_env / ".cache"

		c1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c1.collect_agy()
		db_file = cache_dir / f"cache-v{sba.CACHE_SCHEMA_VERSION}.sqlite"

		def _cached_ids(source: str) -> set[str]:
			conn = sqlite3.connect(db_file)
			try:
				return {r[0] for r in conn.execute("SELECT session_id FROM sessions WHERE source = ?", (source,))}
			finally:
				conn.close()

		assert _cached_ids("agy") == {"cid-keep", "cid-gone"}

		shutil.rmtree(agy_env / "cid-gone")

		c_scoped = sba.AuditCollector(since=None, includes=["cid-keep"], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c_scoped.collect_agy()
		assert _cached_ids("agy") == {"cid-keep", "cid-gone"}

		c_since = sba.AuditCollector(
			since=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
			includes=[],
			excludes=[],
			slow_threshold=20.0,
			max_samples=2,
			cache_dir=cache_dir,
		)
		c_since.collect_agy()
		assert _cached_ids("agy") == {"cid-keep", "cid-gone"}

		c_full = sba.AuditCollector(since=None, includes=[], excludes=["cid-keep"], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		c_full.collect_agy()
		assert _cached_ids("agy") == {"cid-keep"}

		oc_dir = tmp_path / "oc"
		oc_dir.mkdir()
		oc_db = oc_dir / "opencode.db"
		ts1 = int(datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
		conn = sqlite3.connect(oc_db)
		conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, time_updated INTEGER)")
		conn.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
		conn.execute("INSERT INTO session_v2 VALUES ('ses_keep', 'Keep', ?)", (ts1,))
		conn.execute("INSERT INTO session_v2 VALUES ('ses_gone', 'Gone', ?)", (ts1,))
		conn.commit()
		conn.close()

		monkeypatch.setattr(sba, "OPENCODE_DIR", oc_dir)
		oc1 = sba.AuditCollector(since=None, includes=[], excludes=[], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		oc1.collect_opencode()
		assert _cached_ids("opencode") == {"opencode.db:ses_keep", "opencode.db:ses_gone"}

		conn = sqlite3.connect(oc_db)
		conn.execute("DELETE FROM session_v2 WHERE id = 'ses_gone'")
		conn.commit()
		conn.close()

		oc_full = sba.AuditCollector(since=None, includes=[], excludes=["ses_keep"], slow_threshold=20.0, max_samples=2, cache_dir=cache_dir)
		oc_full.collect_opencode()
		assert _cached_ids("opencode") == {"opencode.db:ses_keep"}

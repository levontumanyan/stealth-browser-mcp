import importlib.machinery
import importlib.util
from pathlib import Path


def _load_probe_module():
	path = Path(__file__).resolve().parent / "stealth_probe.py"
	loader = importlib.machinery.SourceFileLoader("stealth_probe_mod", str(path))
	spec = importlib.util.spec_from_loader(loader.name, loader)
	mod = importlib.util.module_from_spec(spec)
	loader.exec_module(mod)
	return mod


probe = _load_probe_module()


def _clean_signals():
	return {
		"webdriver": False,
		"userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/156.0.0.0 Safari/537.36",
		"hasChrome": True,
		"pluginsLength": 5,
		"webglVendor": "Google Inc. (Apple)",
		"webglRenderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2)",
		"screen": {"width": 1440, "height": 900, "availWidth": 1440, "availHeight": 875, "dpr": 2},
		"runtimeLeak": False,
		"cdcProps": [],
	}


def _clean_cmdline():
	return {"found": True, "enable_automation": False, "disable_blink_features": True}


def test_grade_local_probe_pass_degraded_and_fail():
	status, checks = probe.grade_local_probe(_clean_signals(), _clean_cmdline())
	assert status == "PASS"
	assert all(c["status"] == "PASS" for c in checks.values())

	headless_sig = _clean_signals()
	headless_sig["userAgent"] = "Mozilla/5.0 HeadlessChrome/156.0.0.0 Safari/537.36"
	headless_sig["screen"] = {"width": 800, "height": 600, "dpr": 1}
	status_deg, checks_deg = probe.grade_local_probe(headless_sig, _clean_cmdline())
	assert status_deg == "DEGRADED"
	assert checks_deg["user_agent"]["status"] == "DEGRADED"
	assert checks_deg["screen_dimensions"]["status"] == "DEGRADED"

	leak_sig = _clean_signals()
	leak_sig["runtimeLeak"] = True
	status_fail, checks_fail = probe.grade_local_probe(leak_sig, _clean_cmdline())
	assert status_fail == "FAIL"
	assert checks_fail["runtime_enable_leak"]["status"] == "FAIL"

	cmd_fail = {"found": True, "enable_automation": True, "disable_blink_features": True}
	status_cmd, checks_cmd = probe.grade_local_probe(_clean_signals(), cmd_fail)
	assert status_cmd == "FAIL"
	assert checks_cmd["chromium_cmdline_flags"]["status"] == "FAIL"


def test_grade_external_result_unreachable_never_fails():
	res_unreach = probe.grade_external_result(ok=False, error="Navigation error: net::ERR_NAME_NOT_RESOLVED")
	assert res_unreach["status"] == "UNREACHABLE"

	res_pass = probe.grade_external_result(ok=True, title="nowSecure", body_snippet="OH YEAH, you passed!")
	assert res_pass["status"] == "PASS"

	res_fail = probe.grade_external_result(ok=True, title="Just a moment...", body_snippet="Checking your browser")
	assert res_fail["status"] == "FAIL"


def test_probe_cli_parser_has_expected_flags():
	parser = probe.build_parser()
	args = parser.parse_args(["--json", "--external", "--headed", "--port", "9444"])
	assert args.json == "-"
	assert args.external is True
	assert args.headed is True
	assert args.port == 9444

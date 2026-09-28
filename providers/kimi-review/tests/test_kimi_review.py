# -*- coding: utf-8 -*-
"""tests for providers/kimi-review/review.py — all mock, zero real calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review  # noqa: E402
from review import (  # noqa: E402
    MockKimiRunner,
    RouteResult,
    VALID_VERDICTS,
    aggregate,
    build_review_prompt,
    extract_verdict,
    parse_stream_json,
    run_kimi,
    run_review,
    run_route,
    scrub,
)

DIFF = """diff --git a/guardrail.py b/guardrail.py
@@ -1,3 +1,5 @@
+if not vs:
+    return UNKNOWN
"""


def _assistant(payload) -> object:
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    class _P:
        returncode = 0
        stdout = json.dumps({"role": "assistant", "content": content})
        stderr = ""
    return _P


def _verdict(v="approve", findings=None, summary="s"):
    return {"verdict": v, "findings": findings or [], "summary": summary}


# --- wire parsing -----------------------------------------------------------

def test_parse_stream_json_basic():
    out = '\n'.join([
        json.dumps({"role": "meta", "type": "system.version", "version": "0.42.0"}),
        json.dumps({"role": "assistant", "content": "好"}),
        json.dumps({"role": "meta", "type": "session.resume_hint",
                    "session_id": "session_abc"}),
    ])
    text, sid = parse_stream_json(out)
    assert text == "好"
    assert sid == "session_abc"


def test_parse_stream_json_takes_last_assistant_and_ignores_garbage():
    out = "not json at all\n" + json.dumps({"role": "assistant", "content": "first"}) \
        + "\n{broken\n" + json.dumps({"role": "assistant", "content": "final"}) + "\n"
    text, sid = parse_stream_json(out)
    assert text == "final"
    assert sid == ""


# --- verdict extraction -----------------------------------------------------

def test_extract_verdict_from_chatty_prose():
    text = "Sure! Here is my review:\n" + json.dumps(_verdict("reject", [
        {"title": "cmd injection", "severity": "critical", "detail": "shell=True"},
        {"title": "weird", "severity": "banana", "detail": "coerced"},
    ]), ensure_ascii=False) + "\nHope that helps!"
    v = extract_verdict(text)
    assert v is not None and v["verdict"] == "reject"
    assert v["findings"][0]["severity"] == "critical"
    assert v["findings"][1]["severity"] == "info"  # unknown severity coerced


def test_extract_verdict_rejects_invalid_schema():
    assert extract_verdict('{"verdict": "shipit", "findings": []}') is None
    assert extract_verdict("no json here at all") is None
    assert extract_verdict("") is None


def test_extract_verdict_brace_inside_string_not_truncated():
    inner = json.dumps(_verdict("approve", [
        {"title": "uses } inside title", "severity": "low", "detail": "d"},
    ]), ensure_ascii=False)
    v = extract_verdict("Here is my review: " + inner + " Thanks!")
    assert v is not None
    assert v["findings"][0]["title"] == "uses } inside title"


# --- error classification ---------------------------------------------------

def test_run_route_quota_403_classified():
    class _P:
        returncode = 1
        stdout = ""
        stderr = ("error: failed to run prompt: provider.auth_error: 403 You've "
                  "reached your weekly (7-day) usage limit.")
    r = run_route("security", DIFF, runner=lambda *a, **k: _P())
    assert r.ok is False and r.error_class == "quota"
    assert r.verdict is None


def test_run_route_timeout_classified():
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="kimi", timeout=1)
    r = run_route("correctness", DIFF, runner=_boom)
    assert r.ok is False and r.error_class == "timeout"


def test_run_route_model_not_found_classified():
    class _P:
        returncode = 1
        stdout = ""
        stderr = 'error: Model "kimi-code/k2.8-preview" is not configured in config.toml.'
    r = run_route("security", DIFF, runner=lambda *a, **k: _P())
    assert r.error_class == "model_not_found"


def test_run_route_parse_failure_when_no_json():
    r = run_route("security", DIFF, runner=lambda *a, **k: _assistant("I agree, looks good!"))
    assert r.ok is False and r.error_class == "parse"


def test_run_route_happy_path():
    r = run_route("plan", DIFF, runner=lambda *a, **k: _assistant(_verdict("needs_discussion")))
    assert r.ok is True and r.verdict["verdict"] == "needs_discussion"
    assert r.perspective == "plan"


# --- aggregation ------------------------------------------------------------

def _routes(*verdicts):
    out = []
    for i, v in enumerate(verdicts):
        if v is None:
            out.append(RouteResult(["security", "correctness", "plan"][i], False,
                                   error_class="quota", error_raw="403"))
        else:
            out.append(RouteResult(["security", "correctness", "plan"][i], True,
                                   verdict=_verdict(v)))
    return out


def test_aggregate_majority_two_of_three():
    agg = aggregate(_routes("approve", "reject", "approve"), n_expected=3)
    assert agg["verdict"] == "approve"
    assert agg["degraded"] is False
    assert agg["tally"] == {"approve": 2, "reject": 1}


def test_aggregate_split_goes_needs_discussion():
    agg = aggregate(_routes("approve", "reject", "needs_discussion"), n_expected=3)
    assert agg["verdict"] == "needs_discussion"
    assert "no-majority" in agg["reason"]


def test_aggregate_degraded_when_route_fails():
    agg = aggregate(_routes("approve", None, "approve"), n_expected=3)
    assert agg["verdict"] == "approve"          # 2 parsed, both agree
    assert agg["degraded"] is True
    assert agg["failed_routes"][0]["error_class"] == "quota"


def test_aggregate_degraded_split_two_routes():
    agg = aggregate(_routes("approve", None, "reject"), n_expected=3)
    assert agg["verdict"] == "needs_discussion"  # 1-1 split
    assert agg["degraded"] is True


def test_aggregate_no_routes():
    agg = aggregate(_routes(None, None, None), n_expected=3)
    assert agg["verdict"] == "needs_discussion" and agg["reason"] == "no-routes"


def test_aggregate_findings_attributed_and_sorted():
    routes = [
        RouteResult("security", True, verdict=_verdict("approve", [
            {"title": "a", "severity": "high", "detail": "x"}])),
        RouteResult("correctness", True, verdict=_verdict("approve", [
            {"title": "b", "severity": "low", "detail": "y"}])),
        RouteResult("plan", True, verdict=_verdict("approve")),
    ]
    agg = aggregate(routes, n_expected=3)
    assert agg["findings"][0]["route"] == "security"
    assert agg["findings"][0]["severity"] == "high"


# --- orchestrator + budget --------------------------------------------------

def test_run_review_end_to_end_mock():
    runner = MockKimiRunner()
    res = run_review(DIFF, runner=runner)
    assert res["provider"] == "kimi"
    assert res["mock"] is True
    assert res["meta"]["calls"] == 3
    assert len(runner.calls) == 3                       # one real-call-shaped spawn per route
    assert res["verdict"] in VALID_VERDICTS
    assert res["routes"][0]["perspective"] == "security"


def test_run_review_budget_hard_cap():
    with pytest.raises(ValueError):
        run_review(DIFF, perspectives=("security", "correctness", "plan",
                                       "security", "correctness", "plan"))
    with pytest.raises(ValueError):
        run_review(DIFF, perspectives=("security", "nope"))


def test_run_review_degrades_on_one_quota_route():
    runner = MockKimiRunner(fail_perspectives=("correctness",))
    res = run_review(DIFF, runner=runner)
    assert res["degraded"] is True
    assert res["n_failed"] == 1
    assert res["failed_routes"][0]["error_class"] == "quota"


# --- hygiene + prompt + cli -------------------------------------------------

def test_scrub_masks_tokens():
    out = scrub("token sk-abcd1234EFGH5678ijkl and jwt eyJhbGciOiJIUzI1NiIsInR5.c2VjcmV0.sig and deadbeef")
    assert "sk-abcd1234***MASKED***" in out
    assert "eyJhbGciOiJIU***MASKED***" in out
    assert "c2VjcmV0" not in out
    assert "deadbeef" in out  # short hex stays readable


def test_build_review_prompt_lens_and_truncation():
    p = build_review_prompt("security", DIFF, plan_context="make it fail-closed")
    assert "SECURITY review lens" in p and DIFF in p and "fail-closed" in p
    p2 = build_review_prompt("security", "x" * 100, max_diff_chars=10)
    assert "[diff truncated at 10 chars]" in p2
    with pytest.raises(ValueError):
        build_review_prompt("vibes", DIFF)


def test_cli_mock_smoke(tmp_path):
    diff_file = tmp_path / "d.patch"
    diff_file.write_text(DIFF, encoding="utf-8")
    out_file = tmp_path / "v.json"
    rc = review.main(["--diff", str(diff_file), "--mock", "--out", str(out_file)])
    assert rc == 0
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved["mock"] is True and saved["meta"]["calls"] == 3
    assert saved["verdict"] in VALID_VERDICTS


def test_real_invocation_shape_is_stream_json():
    """Guards the wire contract: -m + stream-json, and NO --yolo with -p.

    Evidence (2026-09-29): kimi 0.42.0 rejects `Cannot combine --prompt with
    --yolo.` locally — -p alone is the one-shot headless form.
    """
    seen = {}
    def _fake(argv, **k):
        seen["argv"] = argv
        return _assistant(_verdict())
    run_kimi("hi", exe="kimi-fake", runner=_fake)
    argv = seen["argv"]
    assert "-m" in argv and "kimi-code/kimi-for-coding" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--yolo" not in argv and "--auto" not in argv

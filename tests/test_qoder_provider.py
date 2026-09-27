# coding: utf-8
"""qodercn provider 纯层测试：subprocess 全部 mock，不真调 CLI。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "providers" / "qoder"))

import provider as qoder_provider  # noqa: E402

# 实跑捕获的单行 JSON（免费窗口证据：total_credits=0, total_cost_usd=0）。
SAMPLE_LINE = (
    '{"type":"result","subtype":"success","duration_ms":40030,"duration_api_ms":39967,'
    '"is_error":false,"num_turns":1,"result":"好","stop_reason":"end_turn",'
    '"total_cost_usd":0,"total_credits":0,"usage":{"input_tokens":0,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,'
    '"service_tier":"standard","request_id":"a0473232-86b8-424f-b0a5-70080cdae40e",'
    '"context_usage_ratio":0.10734444444444445},"modelUsage":{"qfmodel":'
    '{"inputTokens":0,"outputTokens":0,"cacheReadInputTokens":0,'
    '"cacheCreationInputTokens":0,"webSearchRequests":0,"costUSD":0,"contextWindow":0,'
    '"maxOutputTokens":0,"credits":0}},"permission_denials":[],"fast_mode_state":"off",'
    '"uuid":"a6bbd4e7-9409-4375-901a-68e0d1b25dc6",'
    '"session_id":"8fa18670-6dad-47a2-ab9f-47473c1009d9"}'
)


class _Completed(object):
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_build_prompt_cases():
    assert qoder_provider.build_prompt(
        [{"role": "user", "content": "只回复一个字"}]
    ) == "只回复一个字"

    # 多轮时只用最后一条 user。
    assert qoder_provider.build_prompt([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "mid"},
        {"role": "user", "content": "second"},
    ]) == "second"

    joined = qoder_provider.build_prompt([{
        "role": "user",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "http://example.invalid/a.png"}},
            {"type": "text", "text": "world"},
        ],
    }])
    assert joined == "helloworld"

    with pytest.raises(ValueError):
        qoder_provider.build_prompt([{"role": "system", "content": "no user here"}])

    with pytest.raises(ValueError):
        qoder_provider.build_prompt([])


def test_run_qodercn_success_parses_sample_and_argv(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        assert qoder_provider.CALL_LOCK.locked()
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _Completed(0, SAMPLE_LINE, "")

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_run)
    res = qoder_provider.run_qodercn("只回复一个字", bin_path="qodercn")

    assert res.ok is True
    assert res.response == "好"
    assert res.total_credits == 0
    assert res.total_cost_usd == 0
    assert res.session_id == "8fa18670-6dad-47a2-ab9f-47473c1009d9"
    assert res.subtype == "success"
    assert res.duration_ms == 40030
    assert captured["cmd"] == [
        "qodercn",
        "-p",
        "只回复一个字",
        "--model",
        "Qwen3.8-Flash",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["timeout"] == qoder_provider.DEFAULT_TIMEOUT_S
    assert captured["kwargs"]["cwd"] is None


def test_run_qodercn_tolerates_stray_lines_and_keeps_last_result(monkeypatch):
    earlier = json.loads(SAMPLE_LINE)
    earlier["result"] = "先出现的结果"
    stdout = "not json at all\n" + json.dumps(earlier, ensure_ascii=False) + "\n" + SAMPLE_LINE + "\n"

    def fake_run(cmd, **kwargs):
        return _Completed(0, stdout, "")

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_run)
    res = qoder_provider.run_qodercn("hi", bin_path="qodercn")
    assert res.ok is True
    assert res.response == "好"


def test_run_qodercn_binary_missing(monkeypatch):
    called = {"n": 0}

    def fake_run(cmd, **kwargs):
        called["n"] += 1
        raise AssertionError("subprocess must not run when the binary is missing")

    monkeypatch.setattr(qoder_provider.shutil, "which", lambda _name: None)
    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_run)
    res = qoder_provider.run_qodercn("hi", bin_path=None)
    assert res.ok is False
    assert "not found" in res.error
    assert called["n"] == 0


def test_run_qodercn_failure_subtype_and_returncode(monkeypatch):
    failed = json.loads(SAMPLE_LINE)
    failed["subtype"] = "error_during_execution"
    failed["is_error"] = True

    def fake_subtype(cmd, **kwargs):
        return _Completed(0, json.dumps(failed), "")

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_subtype)
    res = qoder_provider.run_qodercn("hi", bin_path="qodercn")
    assert res.ok is False
    assert res.subtype == "error_during_execution"

    def fake_rc(cmd, **kwargs):
        return _Completed(2, SAMPLE_LINE, "boom")

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_rc)
    res_rc = qoder_provider.run_qodercn("hi", bin_path="qodercn")
    assert res_rc.ok is False
    assert res_rc.returncode == 2


def test_run_qodercn_malformed_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _Completed(0, "not json at all", "stderr-noise")

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_run)
    res = qoder_provider.run_qodercn("hi", bin_path="qodercn")
    assert res.ok is False
    assert res.raw == {}
    assert "parse" in res.error.lower()


def test_run_qodercn_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_run)
    res = qoder_provider.run_qodercn("hi", bin_path="qodercn", timeout_s=180)
    assert res.ok is False
    assert "timeout" in res.error
    assert "180" in res.error


def test_to_chat_completion_shape():
    res = qoder_provider.QoderRunResult(
        ok=True,
        response="好",
        subtype="success",
        session_id="8fa18670-6dad-47a2-ab9f-47473c1009d9",
        model="Qwen3.8-Flash",
        total_credits=0,
        total_cost_usd=0,
        usage={"input_tokens": 2, "output_tokens": 5},
        duration_ms=40030,
        returncode=0,
    )
    body = qoder_provider.to_chat_completion(res, "Qwen3.8-Flash")
    assert body["id"].startswith("chatcmpl-qoder-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == "Qwen3.8-Flash"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "好"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 5,
        "total_tokens": 7,
    }
    assert body["qoder"]["total_credits"] == 0
    assert body["qoder"]["total_cost_usd"] == 0
    assert body["qoder"]["session_id"] == res.session_id
    assert body["qoder"]["duration_ms"] == 40030


def test_to_openai_usage_missing_tokens_are_zero():
    res = qoder_provider.QoderRunResult(ok=True, usage={})
    assert res.to_openai_usage() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_run_prompt_uses_last_user_message(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["prompt"] = cmd[2]
        return _Completed(0, SAMPLE_LINE, "")

    monkeypatch.setattr(qoder_provider.subprocess, "run", fake_run)
    res = qoder_provider.run_prompt(
        [
            {"role": "system", "content": "ignore"},
            {"role": "user", "content": "最后这条"},
        ],
        bin_path="qodercn",
    )
    assert res.ok is True
    assert captured["prompt"] == "最后这条"


def test_error_body_shape():
    body = qoder_provider.error_body("nope", "upstream_error", "qodercn_failed")
    assert body == {
        "error": {"message": "nope", "type": "upstream_error", "code": "qodercn_failed"}
    }

# coding: utf-8
"""qoder HTTP 服务端到端：临时端口 + 注入 runner，不真调 qodercn。"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "providers" / "qoder"))

import provider as qoder_provider  # noqa: E402
import server as qoder_server  # noqa: E402


def _request(port, method, path, body=None, timeout=10):
    url = "http://127.0.0.1:%s%s" % (port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            payload = json.loads(raw.decode("utf-8")) if raw else None
            return resp.status, ctype, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        ctype = exc.headers.get("Content-Type", "")
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return exc.code, ctype, payload


def _wait_health(port, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, _ctype, payload = _request(port, "GET", "/healthz", timeout=1)
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return
        except Exception as exc:  # 端口尚未 accept
            last = exc
        time.sleep(0.02)
    raise RuntimeError("qoder server did not become ready: %s" % last)


def _ok_result(model, content="好", prompt_tokens=4, completion_tokens=6):
    return qoder_provider.QoderRunResult(
        ok=True,
        response=content,
        subtype="success",
        session_id="8fa18670-6dad-47a2-ab9f-47473c1009d9",
        model=model,
        total_credits=0,
        total_cost_usd=0.0,
        usage={"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
        raw={"type": "result", "subtype": "success"},
        duration_ms=40030,
        returncode=0,
    )


@pytest.fixture
def start_server(tmp_path):
    holders = []

    def _start(runner, log_dir=None):
        directory = tmp_path if log_dir is None else log_dir
        srv = qoder_server.make_server(
            "127.0.0.1", 0, runner=runner, log_dir=directory,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        holders.append((srv, thread))
        port = srv.server_address[1]
        _wait_health(port)
        return port, directory

    yield _start

    for srv, thread in holders:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_post_success(start_server):
    seen = {}

    def runner(messages, model):
        seen["messages"] = messages
        seen["model"] = model
        return _ok_result(model)

    port, _log_dir = start_server(runner)
    status, ctype, payload = _request(port, "POST", "/v1/chat/completions", {
        "model": "Qwen3.8-Flash",
        "messages": [{"role": "user", "content": "只回复一个字"}],
    })
    assert status == 200
    assert "application/json" in ctype
    assert payload["choices"][0]["message"]["content"] == "好"
    assert payload["model"] == "Qwen3.8-Flash"
    usage = payload["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["total_tokens"] == 10
    assert payload["qoder"]["total_credits"] == 0
    assert payload["qoder"]["total_cost_usd"] == 0.0
    assert seen["model"] == "Qwen3.8-Flash"


def test_post_without_user_message_is_400_before_runner(start_server):
    called = {"n": 0}

    def runner(messages, model):
        called["n"] += 1
        return _ok_result(model)

    port, _log_dir = start_server(runner)
    status, ctype, payload = _request(port, "POST", "/v1/chat/completions", {
        "model": "Qwen3.8-Flash",
        "messages": [{"role": "system", "content": "no user"}],
    })
    assert status == 400
    assert "application/json" in ctype
    assert "message" in payload["error"]
    assert payload["error"]["message"]
    assert called["n"] == 0

    status_missing, _ctype, missing = _request(port, "POST", "/v1/chat/completions", {})
    assert status_missing == 400
    assert "message" in missing["error"]
    assert called["n"] == 0


def test_post_binary_missing_is_500(start_server):
    def runner(messages, model):
        return qoder_provider.QoderRunResult(
            ok=False,
            error="qodercn binary not found on PATH",
        )

    port, _log_dir = start_server(runner)
    status, _ctype, payload = _request(port, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 500
    assert payload["error"]["type"] == "internal_error"
    assert payload["error"]["code"] == "provider_binary_missing"


def test_post_upstream_failure_is_502(start_server):
    def runner(messages, model):
        return qoder_provider.QoderRunResult(
            ok=False,
            subtype="error_during_execution",
            error="subtype error_during_execution",
        )

    port, _log_dir = start_server(runner)
    status, _ctype, payload = _request(port, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 502
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == "qodercn_failed"
    assert "message" in payload["error"]


def test_routes_models_health_404_405(start_server):
    def runner(messages, model):
        raise AssertionError("runner must not be called for these routes")

    port, _log_dir = start_server(runner)

    status, _ctype, models = _request(port, "GET", "/v1/models")
    assert status == 200
    assert models["object"] == "list"
    assert models["data"][0]["id"] == qoder_provider.DEFAULT_MODEL
    assert models["data"][0]["object"] == "model"

    status, _ctype, health = _request(port, "GET", "/healthz")
    assert status == 200
    assert health["status"] == "ok"
    assert health["provider"] == "qoder"
    assert health["model"] == qoder_provider.DEFAULT_MODEL

    status, _ctype, missing = _request(port, "GET", "/no-such-path")
    assert status == 404
    assert "message" in missing["error"]

    status, _ctype, denied = _request(port, "PUT", "/v1/chat/completions")
    assert status == 405
    assert "message" in denied["error"]


def test_request_log_omits_prompt_and_response(start_server):
    prompt = "UNIQUE_PROMPT_DO_NOT_LOG_zz9"
    response = "UNIQUE_RESPONSE_DO_NOT_LOG_qq8"

    def runner(messages, model):
        return _ok_result(model, content=response, prompt_tokens=1, completion_tokens=1)

    port, log_dir = start_server(runner)
    status, _ctype, payload = _request(port, "POST", "/v1/chat/completions", {
        "model": "Qwen3.8-Flash",
        "messages": [{"role": "user", "content": prompt}],
    })
    assert status == 200
    assert payload["choices"][0]["message"]["content"] == response

    log_path = pathlib.Path(log_dir) / "requests.jsonl"
    text = log_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ts" in record
    assert record["status"] == 200
    assert record["total_credits"] == 0
    assert record["total_cost_usd"] == 0.0
    assert record["prompt_chars"] == len(prompt)
    assert record["model"] == "Qwen3.8-Flash"
    assert "subtype" in record
    assert "duration_ms" in record
    assert "error" in record
    assert "remote" in record
    assert prompt not in text
    assert response not in text


def test_unexpected_runner_exception_is_500_without_traceback(start_server):
    def runner(messages, model):
        raise RuntimeError("SECRET_TRACEBACK_TOKEN prompt was hello")

    port, _log_dir = start_server(runner)
    status, _ctype, payload = _request(port, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert status == 500
    assert payload["error"]["code"] == "internal_error"
    blob = json.dumps(payload, ensure_ascii=False)
    assert "Traceback" not in blob
    assert "SECRET_TRACEBACK_TOKEN" not in blob

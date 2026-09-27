"""pytest for providers/zcode — all mocked; the real zcode CLI is never spawned.

Run:  python -m pytest providers/zcode/tests -q
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROV_DIR = HERE.parent
sys.path.insert(0, str(PROV_DIR))

import zcode_provider as zp  # noqa: E402
import adapter_server as adapter  # noqa: E402


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def result_event(session_id="sess_t1", response="好", trace_id="tr_1", **usage):
    ev = {"type": "result", "sessionId": session_id, "traceId": trace_id,
          "response": response, "usage": usage or {"inputTokens": 100, "outputTokens": 1},
          "projection": {"status": "completed", "turnCount": 1}}
    return json.dumps(ev)


# ---------------------------------------------------------------------------
# launch knowledge
# ---------------------------------------------------------------------------

def test_build_turn_command_first_turn_has_no_resume():
    argv = zp.build_turn_command("hi")
    assert argv[0] == "zcode"
    assert "--resume" not in argv
    assert argv[-2:] == ["-p", "hi"]
    assert "--mode" in argv and "yolo" in argv and "stream-json" in argv


def test_build_turn_command_resume_emits_session_flag():
    argv = zp.build_turn_command("hi", session_id="sess_x", first_turn=False)
    assert argv[argv.index("--resume") + 1] == "sess_x"
    assert argv[-1] == "hi"


def test_is_turn_complete_structural():
    assert zp.is_turn_complete(result_event())
    assert zp.is_turn_complete('  {"type":  "result"}  ')  # spacing variants tolerated
    assert not zp.is_turn_complete('{"type":"session.updated"}')
    assert not zp.is_turn_complete("not json at all")
    assert not zp.is_turn_complete("{broken")


# ---------------------------------------------------------------------------
# run_prompt parsing (fake runner)
# ---------------------------------------------------------------------------

def test_run_prompt_parses_result_and_usage():
    stream = "\n".join([
        json.dumps({"type": "session.updated"}),
        result_event(session_id="sess_ok", response="好",
                     inputTokens=22649, outputTokens=23, totalTokens=22672),
        "",
    ])
    calls = {}

    def fake_runner(argv, **kw):
        calls["argv"] = argv
        calls["cwd"] = kw.get("cwd")
        return FakeProc(stdout=stream, stderr="", returncode=0)

    res = zp.run_prompt("只回复一个字：好", cwd="/w", zcode_bin="/fake/zcode", runner=fake_runner)
    assert res.ok is True
    assert res.response == "好"
    assert res.session_id == "sess_ok"
    assert res.usage["totalTokens"] == 22672
    assert res.projection["status"] == "completed"
    assert "result" in res.event_types
    assert calls["argv"][-2:] == ["-p", "只回复一个字：好"]
    assert calls["cwd"] == "/w"
    d = res.to_provider_result()
    assert d["provider"] == "zcode" and d["ok"] is True and d["result"] == "好"


def test_run_prompt_no_result_event_is_error():
    stream = json.dumps({"type": "session.updated"})
    res = zp.run_prompt("hi", zcode_bin="/fake/zcode",
                        runner=lambda argv, **kw: FakeProc(stdout=stream, returncode=0))
    assert res.ok is False
    assert "no result event" in res.error


def test_run_prompt_nonzero_exit_with_result_is_not_ok():
    res = zp.run_prompt("hi", zcode_bin="/fake/zcode",
                        runner=lambda argv, **kw: FakeProc(stdout=result_event(), returncode=1))
    assert res.ok is False and res.response == "好"  # parsed but flagged failed


def test_run_prompt_missing_binary_short_circuits(monkeypatch):
    monkeypatch.setattr(zp.shutil, "which", lambda name: None)
    res = zp.run_prompt("hi")
    assert res.ok is False and "not found" in res.error


def test_run_prompt_timeout_reported(monkeypatch):
    def slow_runner(argv, **kw):
        raise zp.subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])
    res = zp.run_prompt("hi", timeout_s=1, zcode_bin="/fake/zcode", runner=slow_runner)
    assert res.ok is False and "timeout after 1s" in res.error


def test_run_prompt_log_masks_secretish_values(tmp_path):
    stream = json.dumps({"type": "result", "sessionId": "s",
                         "response": "r", "usage": {}, "apiKey": "supersecret123"})
    log = tmp_path / "turn.json"
    zp.run_prompt("task", zcode_bin="/fake/zcode", log_path=str(log),
                  runner=lambda argv, **kw: FakeProc(stdout=stream, returncode=0))
    text = log.read_text(encoding="utf-8")
    assert "supersecret123" not in text
    assert "<masked>" in text


# ---------------------------------------------------------------------------
# B-1 adapter: mapping + HTTP wire
# ---------------------------------------------------------------------------

def test_messages_to_prompt_drops_system_and_prefixes_roles():
    prompt, dropped = adapter.messages_to_prompt([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": [{"type": "text", "text": "again"}]},
    ])
    assert dropped == 1
    assert prompt == "hello\n[assistant] hi\n[user] again"
    prompt2, dropped2 = adapter.messages_to_prompt([{"role": "system", "content": "x"}])
    assert prompt2 == "" and dropped2 == 1


def test_usage_mapping_keeps_zcode_counters_as_evidence():
    u = adapter.zcode_usage_to_openai({"inputTokens": 22649, "outputTokens": 23,
                                       "totalTokens": 22672, "cacheReadTokens": 15616})
    assert u["prompt_tokens"] == 22649
    assert u["completion_tokens"] == 23
    assert u["total_tokens"] == 22672
    assert u["zcode_usage"]["cacheReadTokens"] == 15616


def test_handle_chat_completions_success_and_session_state():
    seen = []
    sessions = iter(["sess_n1", "sess_n2"])

    def fake_turn(prompt, session_id, first_turn):
        seen.append((prompt, session_id, first_turn))
        return zp.ZcodeRunResult(ok=True, response="好", session_id=next(sessions),
                                 trace_id="t9", usage={"inputTokens": 10, "outputTokens": 1})

    body = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "只回复一个字：好"}]}
    out = adapter.handle_chat_completions(body, turn_fn=fake_turn)
    assert seen[0] == ("只回复一个字：好", None, True)
    assert out["choices"][0]["message"]["content"] == "好"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["id"].startswith("chatcmpl-zcode-")
    assert adapter.STATE["session_id"] == "sess_n1"

    out2 = adapter.handle_chat_completions(body, turn_fn=fake_turn)
    assert seen[1] == ("只回复一个字：好", "sess_n1", False)  # second turn resumes the session
    assert adapter.STATE["session_id"] == "sess_n2"


def test_handle_chat_completions_error_paths():
    out_empty = adapter.handle_chat_completions(
        {"messages": [{"role": "system", "content": "only system"}]}, turn_fn=lambda *a: None)
    assert out_empty == {"__status": 400, "__error": out_empty["__error"]}
    assert "no usable" in out_empty["__error"]

    def failing_turn(prompt, session_id, first_turn):
        return zp.ZcodeRunResult(ok=False, error="boom")
    out_fail = adapter.handle_chat_completions(
        {"messages": [{"role": "user", "content": "x"}]}, turn_fn=failing_turn)
    assert out_fail["__status"] == 502 and "boom" in out_fail["__error"]


def _http_json(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 4xx/5xx: status + JSON error body
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_end_to_end_on_ephemeral_port(monkeypatch):
    """Real HTTP server, fake zcode turn — exercises the full wire contract."""
    stream_state = {"turns": 0}

    def fake_run_prompt(task, **kw):
        stream_state["turns"] += 1
        sid = "sess_http_{}".format(stream_state["turns"])
        return zp.ZcodeRunResult(
            ok=True, response="好", session_id=sid, trace_id="tr{}".format(stream_state["turns"]),
            usage={"inputTokens": 8, "outputTokens": 1, "totalTokens": 9})

    monkeypatch.setattr(adapter, "run_prompt", fake_run_prompt)
    adapter.STATE["session_id"] = None
    server = adapter.make_server("127.0.0.1", 0)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        base = "http://127.0.0.1:{}/v1".format(port)
        status, models = _http_json("GET", base + "/models")
        assert status == 200 and models["data"][0]["id"] == adapter.MODEL_ID

        status, health = _http_json("GET", "http://127.0.0.1:{}/healthz".format(port))
        assert status == 200 and health["ok"] is True

        status, comp = _http_json("POST", base + "/chat/completions",
                                  {"messages": [{"role": "user", "content": "只回复一个字：好"}]})
        assert status == 200
        assert comp["choices"][0]["message"]["content"] == "好"
        assert comp["usage"]["total_tokens"] == 9

        status, err = _http_json("POST", base + "/nope", {})
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_harness_session_continuity(monkeypatch):
    calls = []

    def fake_run_prompt(task, **kw):
        calls.append((kw.get("session_id"), kw.get("first_turn")))
        return zp.ZcodeRunResult(ok=True, response="ok", session_id="sess_h1")
    monkeypatch.setattr(zp, "run_prompt", fake_run_prompt)

    h = zp.ZcodeHarnessProvider.create({"cwd": "/w"})
    assert h.card["id"] == "zcode"
    h.turn("a")
    h.turn("b")
    assert calls == [(None, True), ("sess_h1", False)]
    assert list(h.events())[-1]["result"] == "ok"
    assert list(h.turn_events())[-1]["session_id"] == "sess_h1"

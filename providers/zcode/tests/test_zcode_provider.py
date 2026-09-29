"""pytest for providers/zcode — all mocked; the real zcode CLI is never spawned.

Run:  python -m pytest providers/zcode/tests -q
"""

import json
import sys
import threading
import time
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


# ---------------------------------------------------------------------------
# 线2 long-run mode: --mode build/edit/plan/yolo
# ---------------------------------------------------------------------------

def test_validate_mode_accepts_official_values_and_rejects_others():
    for m in ("build", "edit", "plan", "yolo"):
        assert zp.validate_mode(m) == m
    with pytest.raises(ValueError):
        zp.validate_mode("stealth")
    with pytest.raises(ValueError):
        zp.validate_mode(None)
    with pytest.raises(ValueError):
        zp.validate_mode("YOLO")  # case-sensitive, mirrors argv passthrough


def test_build_turn_command_mode_passthrough_keeps_prompt_last():
    for m in ("build", "edit", "plan", "yolo"):
        argv = zp.build_turn_command("hi", mode=m)
        assert argv[argv.index("--mode") + 1] == m
        assert argv[-2:] == ["-p", "hi"]


def test_build_turn_command_invalid_mode_raises_without_argv():
    with pytest.raises(ValueError):
        zp.build_turn_command("hi", mode="nope")


def test_run_prompt_invalid_mode_short_circuits_before_spawn():
    def must_not_spawn(argv, **kw):  # noqa: ARG001
        raise AssertionError("CLI must not be spawned on invalid mode")
    res = zp.run_prompt("hi", mode="nope", zcode_bin="/fake/zcode", runner=must_not_spawn)
    assert res.ok is False and "mode must be one of" in res.error


def test_run_prompt_mode_reaches_argv():
    seen = {}

    def fake_runner(argv, **kw):
        seen["argv"] = argv
        return FakeProc(stdout=result_event(session_id="s1"), returncode=0)
    res = zp.run_prompt("hi", mode="plan", zcode_bin="/fake/zcode", runner=fake_runner)
    assert res.ok is True
    assert seen["argv"][seen["argv"].index("--mode") + 1] == "plan"


# ---------------------------------------------------------------------------
# 线2 stream-json event stream: run_prompt_streaming (fake Popen)
# ---------------------------------------------------------------------------

class FakePopen:
    """Minimal Popen-alike: .stdout iterable of lines, .stderr str, .wait()/.kill()."""

    def __init__(self, lines, returncode=0, stderr="", delay_s=0.0):
        self._lines = list(lines)
        self.stderr = stderr
        self._returncode = returncode
        self._delay_s = delay_s
        self.killed = False
        self.stdout = self._gen()

    def _gen(self):
        for ln in self._lines:
            if self.killed:
                return
            if self._delay_s:
                time.sleep(self._delay_s)
            yield ln

    def wait(self, timeout=None):  # noqa: ARG002
        return self._returncode

    def kill(self):
        self.killed = True


def test_run_prompt_streaming_parses_events_live_via_on_event():
    lines = [
        json.dumps({"type": "session.updated", "modelId": "GLM-5.3-Flash"}) + "\n",
        json.dumps({"type": "tool.discovered", "tool": "read"}) + "\n",
        result_event(session_id="sess_live", response="好") + "\n",
    ]

    def fake_popen(argv, **kw):  # noqa: ARG001
        return FakePopen(lines, returncode=0)

    seen = []
    res = zp.run_prompt_streaming("hi", mode="build", zcode_bin="/fake/zcode",
                                  popen=fake_popen, on_event=seen.append)
    assert res.ok is True and res.response == "好" and res.session_id == "sess_live"
    assert [e["type"] for e in seen] == ["session.updated", "tool.discovered", "result"]
    assert res.event_types == ["session.updated", "tool.discovered", "result"]


def test_run_prompt_streaming_resume_flag_emitted():
    def fake_popen(argv, **kw):
        FakePopen.argv = argv
        return FakePopen([result_event(session_id="sess_n2") + "\n"], returncode=0)
    res = zp.run_prompt_streaming("next", session_id="sess_n1", first_turn=False,
                                  zcode_bin="/fake/zcode", popen=fake_popen)
    assert res.ok is True
    argv = FakePopen.argv
    assert argv[argv.index("--resume") + 1] == "sess_n1"


def test_run_prompt_streaming_timeout_kills_process():
    # each line blocks ~0.2s; timeout 0 boundary forces kill on first check
    def fake_popen(argv, **kw):  # noqa: ARG001
        return FakePopen([result_event() + "\n"] * 5, returncode=0, delay_s=0.2)

    res = zp.run_prompt_streaming("hi", timeout_s=0, zcode_bin="/fake/zcode", popen=fake_popen)
    assert res.ok is False and "timeout after 0s" in res.error


def test_run_prompt_streaming_invalid_mode_short_circuits():
    def must_not_spawn(argv, **kw):  # noqa: ARG001
        raise AssertionError("CLI must not be spawned on invalid mode")
    res = zp.run_prompt_streaming("hi", mode="bogus", zcode_bin="/fake/zcode", popen=must_not_spawn)
    assert res.ok is False and "mode must be one of" in res.error


def test_run_prompt_streaming_nonzero_exit_flagged():
    def fake_popen(argv, **kw):  # noqa: ARG001
        return FakePopen([result_event(response="done") + "\n"], returncode=3, stderr="boom")
    res = zp.run_prompt_streaming("hi", zcode_bin="/fake/zcode", popen=fake_popen)
    assert res.ok is False and res.response == "done" and "boom" in res.stderr_tail


def test_harness_turn_mode_and_stream_surface(monkeypatch):
    calls = []
    stream_events = []

    def fake_streaming(task, **kw):
        calls.append(("stream", kw.get("mode"), kw.get("session_id"), kw.get("first_turn")))
        ev = {"type": "result", "sessionId": "sess_st1", "response": "ok"}
        stream_events.append(ev)
        if kw.get("on_event"):
            kw["on_event"](ev)
        out = zp.ZcodeRunResult(ok=True, response="ok", session_id="sess_st1")
        out.raw_events = [ev]
        return out

    def fake_run(task, **kw):
        calls.append(("run", kw.get("mode"), kw.get("session_id"), kw.get("first_turn")))
        return zp.ZcodeRunResult(ok=True, response="ok2", session_id="sess_st1")

    monkeypatch.setattr(zp, "run_prompt_streaming", fake_streaming)
    monkeypatch.setattr(zp, "run_prompt", fake_run)

    h = zp.ZcodeHarnessProvider.create({"mode": "plan"})
    assert h.card["capabilities"]["modes"] == ["build", "edit", "plan", "yolo"]
    h.turn("a", stream=True)                     # config mode plan, streaming path
    h.turn("b", mode="yolo")                     # per-turn override, plain path
    assert calls == [("stream", "plan", None, True), ("run", "yolo", "sess_st1", False)]
    assert h.last_stream_events == [{"type": "result", "sessionId": "sess_st1", "response": "ok"}]


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
    adapter.STATE["session_id"] = None  # module state persists across tests
    seen = []
    sessions = iter(["sess_n1", "sess_n2"])

    def fake_turn(prompt, session_id, first_turn, mode):
        seen.append((prompt, session_id, first_turn, mode))
        return zp.ZcodeRunResult(ok=True, response="好", session_id=next(sessions),
                                 trace_id="t9", usage={"inputTokens": 10, "outputTokens": 1})

    body = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "只回复一个字：好"}]}
    out = adapter.handle_chat_completions(body, turn_fn=fake_turn)
    assert seen[0] == ("只回复一个字：好", None, True, adapter.ADAPTER_MODE)
    assert out["choices"][0]["message"]["content"] == "好"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["id"].startswith("chatcmpl-zcode-")
    assert adapter.STATE["session_id"] == "sess_n1"

    out2 = adapter.handle_chat_completions(body, turn_fn=fake_turn)
    assert seen[1] == ("只回复一个字：好", "sess_n1", False, adapter.ADAPTER_MODE)  # resumes session
    assert adapter.STATE["session_id"] == "sess_n2"


def test_handle_chat_completions_mode_passthrough_and_invalid_400():
    seen = {}

    def fake_turn(prompt, session_id, first_turn, mode):
        seen["mode"] = mode
        return zp.ZcodeRunResult(ok=True, response="ok", session_id="sess_m1")

    body = {"messages": [{"role": "user", "content": "x"}], "mode": "plan"}
    out = adapter.handle_chat_completions(body, turn_fn=fake_turn)
    assert out["choices"][0]["message"]["content"] == "ok"
    assert seen["mode"] == "plan"

    bad = adapter.handle_chat_completions(
        {"messages": [{"role": "user", "content": "x"}], "mode": "stealth"},
        turn_fn=fake_turn)
    assert bad == {"__status": 400, "__error": bad["__error"]}
    assert "mode must be one of" in bad["__error"]


def test_handle_chat_completions_session_new_forks_fresh_session():
    adapter.STATE["session_id"] = None  # module state persists across tests
    seen = []

    def fake_turn(prompt, session_id, first_turn, mode):
        seen.append((session_id, first_turn))
        return zp.ZcodeRunResult(ok=True, response="ok", session_id="sess_f{}".format(len(seen)))

    body = {"messages": [{"role": "user", "content": "x"}]}
    adapter.handle_chat_completions(dict(body), turn_fn=fake_turn)  # establishes sess_f1
    adapter.handle_chat_completions(dict(body, session="new"), turn_fn=fake_turn)
    assert seen == [(None, True), (None, True)]  # fork restarts the session chain
    assert adapter.STATE["session_id"] == "sess_f2"


def test_handle_chat_completions_error_paths():
    out_empty = adapter.handle_chat_completions(
        {"messages": [{"role": "system", "content": "only system"}]}, turn_fn=lambda *a: None)
    assert out_empty == {"__status": 400, "__error": out_empty["__error"]}
    assert "no usable" in out_empty["__error"]

    def failing_turn(prompt, session_id, first_turn, mode):
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


def _http_raw(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def test_http_end_to_end_on_ephemeral_port(monkeypatch):
    """Real HTTP server, fake zcode turn — exercises the full wire contract."""
    stream_state = {"turns": 0}

    def fake_run_prompt_streaming(task, **kw):
        stream_state["turns"] += 1
        sid = "sess_http_{}".format(stream_state["turns"])
        res = zp.ZcodeRunResult(
            ok=True, response="好", session_id=sid, trace_id="tr{}".format(stream_state["turns"]),
            usage={"inputTokens": 8, "outputTokens": 1, "totalTokens": 9})
        res.raw_events = [{"type": "result", "sessionId": sid}]
        return res

    monkeypatch.setattr(adapter, "run_prompt_streaming", fake_run_prompt_streaming)
    adapter.STATE["session_id"] = None
    adapter.EVENTS.clear()
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
        assert health["mode"] == adapter.ADAPTER_MODE
        assert health["modes"] == ["build", "edit", "plan", "yolo"]

        status, comp = _http_json("POST", base + "/chat/completions",
                                  {"messages": [{"role": "user", "content": "只回复一个字：好"}]})
        assert status == 200
        assert comp["choices"][0]["message"]["content"] == "好"
        assert comp["usage"]["total_tokens"] == 9

        # /v1/events: NDJSON ring carries at least the turn.completed terminator
        status, ctype, body_text = _http_raw("http://127.0.0.1:{}/v1/events".format(port))
        assert status == 200 and ctype.startswith("application/x-ndjson")
        events = [json.loads(ln) for ln in body_text.splitlines() if ln.strip()]
        assert events, "event ring must not be empty after a turn"
        assert any(e["type"] == "turn.completed" and e.get("ok") is True for e in events)
        assert all("content" not in e for e in events)  # metadata only, no prompt/response

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

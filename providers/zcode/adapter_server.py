"""adapter_server.py — B-1 local OpenAI-compatible HTTP shell over the zcode provider.

Minimal stdlib HTTP server bridging OpenAI-chat clients (jiuwenswarm
model_client_config: client_provider=OpenAI, or anything speaking
POST /v1/chat/completions) to the official zcode CLI via zcode_provider.run_prompt():

    client --(OpenAI chat.completions)--> this adapter
      -> spawn `zcode -p <prompt> [--resume <sess>] --mode yolo --output-format stream-json`
      -> parse NDJSON, terminator {"type":"result", sessionId, response, usage,...}
      -> translate to OpenAI chat.completion response

Wire contract (the minimum a chat client needs):
  POST /v1/chat/completions   non-streaming JSON; stream=true -> single-chunk SSE
  GET  /v1/models             static single-model list
  GET  /v1/events             NDJSON event stream (stream-json types, ring buffer)
  GET  /healthz               liveness + counters

Mapping decisions (documented, evidence-first — verified end-to-end 2026-09-27):
- messages -> prompt: non-system messages are flattened with role prefixes; system
  messages are DROPPED on purpose — the agentic system prompt belongs to zcode itself,
  that is the whole B-1 design (zcode acts as the agent harness behind an OpenAI facade).
- session reuse: the adapter keeps ONE zcode sessionId in memory; the first turn
  creates it, later turns `--resume` it, so provider-side prompt caching applies
  across requests (note: current zcode CLI forks a new sessionId on --resume while
  keeping cache hits). A request may pass non-standard `"session": "new"` to fork
  a fresh session (long-run job boundary).
- long-run mode: requests may pass non-standard `"mode": "build"|"edit"|"plan"|"yolo"`
  (official --mode values, arguments.ts:15); default from ZCODE_ADAPTER_MODE (yolo).
  Invalid modes are rejected 400 before any spawn.
- event stream: turns run through run_prompt_streaming(); every parsed stream-json
  event lands in a bounded ring buffer exposed as NDJSON at GET /v1/events
  (type/session/ok metadata only — no prompt or response content).
- usage mapping: inputTokens->prompt_tokens, outputTokens->completion_tokens,
  totalTokens->total_tokens; cache/reasoning counters kept under `usage.zcode_usage`.

Credentials: none here. The adapter never reads ~/.zcode; the spawned zcode CLI uses
the user's own login state (same as GUI), which is the structural quota argument.

Environment overrides: ZCODE_ADAPTER_HOST (default 127.0.0.1), ZCODE_ADAPTER_PORT
(default 8123), ZCODE_ADAPTER_MODEL (default GLM-5.3), ZCODE_ADAPTER_MODE (default
yolo), ZCODE_ADAPTER_CWD (default: directory the server was started from),
ZCODE_ADAPTER_TIMEOUT (default 300 s).
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # package-relative import (providers.zcode) with same-dir fallback
    from .zcode_provider import (  # type: ignore
        DEFAULT_MODE, ZCODE_MODES, run_prompt_streaming, validate_mode,
    )
except ImportError:  # pragma: no cover - direct-script invocation
    from zcode_provider import (  # type: ignore
        DEFAULT_MODE, ZCODE_MODES, run_prompt_streaming, validate_mode,
    )

HOST = os.environ.get("ZCODE_ADAPTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ZCODE_ADAPTER_PORT", "8123"))
MODEL_ID = os.environ.get("ZCODE_ADAPTER_MODEL", "GLM-5.3")
ADAPTER_MODE = validate_mode(os.environ.get("ZCODE_ADAPTER_MODE", DEFAULT_MODE))
CWD = os.environ.get("ZCODE_ADAPTER_CWD") or os.getcwd()
TIMEOUT_S = int(os.environ.get("ZCODE_ADAPTER_TIMEOUT", "300"))

LOCK = threading.Lock()  # serialize zcode CLI turns (session resume is stateful)

STATE = {"session_id": None}
STATS = {"requests": 0, "zcode_turns": 0, "errors": 0}
EVENTS = collections.deque(maxlen=500)  # stream-json metadata ring (no content)


def record_event(event, session_id="", ok=None):
    """Append one stream-json event's metadata to the ring buffer (no content)."""
    EVENTS.append({
        "ts": time.time(),
        "type": str(event.get("type")) if isinstance(event, dict) else "unknown",
        "session_id": session_id,
        **({} if ok is None else {"ok": bool(ok)}),
    })


def _content_to_text(content) -> str:
    """OpenAI content: str or list[{type:text,...}] -> plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        return "\n".join(x for x in parts if x)
    return str(content)


def messages_to_prompt(messages) -> tuple:
    """Flatten to a prompt; drop system messages (agentic system prompt = zcode's own).

    Returns (prompt, dropped_system_count).
    """
    lines = []
    dropped = 0
    for m in messages or []:
        role = str(m.get("role", "user"))
        text = _content_to_text(m.get("content"))
        if role == "system":
            dropped += 1
            continue
        # collapse consecutive messages; single user message stays verbatim
        lines.append(text if not lines else "[{}] {}".format(role, text))
    return ("\n".join(lines)).strip(), dropped


def zcode_usage_to_openai(usage: dict) -> dict:
    u = usage or {}
    return {
        "prompt_tokens": int(u.get("inputTokens") or 0),
        "completion_tokens": int(u.get("outputTokens") or 0),
        "total_tokens": int(u.get("totalTokens") or 0),
        # non-standard, tolerated by openai SDK (extra=allow): kept as evidence
        "zcode_usage": {
            k: u.get(k)
            for k in (
                "modelRequestCount", "inputTokens", "outputTokens", "totalTokens",
                "cacheReadTokens", "cacheWriteTokens", "reasoningTokens",
            )
        },
    }


def handle_chat_completions(body: dict, *, turn_fn=None) -> dict:
    """One chat.completion through the zcode CLI.

    ``turn_fn`` injects the zcode turn (tests fake it); default runs the real CLI
    via run_prompt_streaming() so stream-json events feed the /v1/events ring.
    Returns an OpenAI completion dict, or {"__status": <int>, "__error": <str>}.

    Non-standard (tolerated) request fields:
      "mode": "build"|"edit"|"plan"|"yolo"  -> zcode --mode (400 on invalid)
      "session": "new"                       -> fork a fresh zcode session
    """
    messages = body.get("messages") or []
    prompt, dropped_system = messages_to_prompt(messages)
    if not prompt:
        return {"__status": 400, "__error": "no usable (non-system) message content"}

    try:
        mode = validate_mode(body.get("mode", ADAPTER_MODE))
    except ValueError as exc:
        return {"__status": 400, "__error": str(exc)}
    fork_new = body.get("session") == "new"

    with LOCK:
        if fork_new:
            STATE["session_id"] = None
        first = STATE["session_id"] is None
        current_session = STATE["session_id"]
        if turn_fn is None:
            def turn_fn(prompt, session_id, first_turn, mode):  # noqa: E306
                return run_prompt_streaming(
                    prompt, cwd=CWD, timeout_s=TIMEOUT_S, mode=mode,
                    session_id=session_id, first_turn=first_turn,
                    on_event=lambda ev: record_event(ev, session_id or ""),
                )
        res = turn_fn(prompt, current_session, first, mode)
        STATS["zcode_turns"] += 1
        if res.session_id:
            STATE["session_id"] = res.session_id
        # single synthetic terminator per turn (live events already fed by on_event;
        # injected turn_fns in tests carry empty raw_events, so no double-record)
        EVENTS.append({
            "ts": time.time(),
            "type": "turn.completed",
            "session_id": getattr(res, "session_id", "") or "",
            "ok": bool(getattr(res, "ok", False)),
        })

    if not res.ok:
        STATS["errors"] += 1
        return {"__status": 502, "__error": "zcode turn failed: {}".format(res.error or res.stderr_tail[-300:])}

    usage = zcode_usage_to_openai(res.usage)
    now = int(time.time())
    return {
        "id": "chatcmpl-zcode-{}".format(res.trace_id or now),
        "object": "chat.completion",
        "created": now,
        "model": body.get("model") or MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": res.response or ""},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet default access log
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json({"ok": True, "zcode_session": STATE["session_id"],
                             "mode": ADAPTER_MODE, "modes": list(ZCODE_MODES), **STATS})
        elif self.path.rstrip("/") == "/v1/models":
            self._send_json({"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "owned_by": "zcode-adapter"}]})
        elif self.path.rstrip("/") == "/v1/events":
            # stream-json event ring as NDJSON (metadata only — no prompt/response content)
            payload = "".join(
                json.dumps(rec, ensure_ascii=False) + "\n" for rec in EVENTS
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json({"error": {"message": "not found"}}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:
            self._send_json({"error": {"message": "bad json: {}".format(exc)}}, 400)
            return
        STATS["requests"] += 1
        stream = bool(body.get("stream"))
        result = handle_chat_completions(body)
        if "__status" in result:
            self._send_json({"error": {"message": result["__error"]}}, result["__status"])
            return
        if stream:
            self._send_sse(result)
        else:
            self._send_json(result)

    def _send_sse(self, completion: dict) -> None:
        """Single-chunk SSE fallback (content arrives whole; usage in final chunk)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def data(obj) -> None:
            self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        base = {k: completion.get(k) for k in ("id", "created", "model")}
        data({**base, "object": "chat.completion.chunk",
              "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        content = completion["choices"][0]["message"]["content"]
        data({**base, "object": "chat.completion.chunk",
              "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
        data({**base, "object": "chat.completion.chunk",
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
              "usage": completion.get("usage")})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    server = make_server()
    print("zcode B-1 adapter listening on http://{}:{} (model={}, mode={})".format(
        server.server_address[0], server.server_address[1], MODEL_ID, ADAPTER_MODE),
        flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

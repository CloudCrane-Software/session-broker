"""zcode_provider.py — ZCode CLI wrapped as a spawn-family agent provider.

Promoted from the route-B verification script (harness-research verify/zcode,
2026-09-26/27, PASS end-to-end with real model calls). Seam alignment with
openjiuwen harness_protocol SPI is preserved as documentation; this module runs
standalone on the Python 3.9+ standard library (no openjiuwen dependency).

Wire shape (evidence: zcode-app-cli 3.14.3 / repos/ZCode @ 29628c9):

    zcode --resume <sid> -p <task> --mode yolo --output-format stream-json
      -> NDJSON events on stdout, terminator {"type":"result", sessionId,
         traceId, response, usage, projection}

Credentials: none here. The spawned zcode CLI uses the user's own login state
(~/.zcode, BigModel OAuth), same as the GUI — that is the structural quota
argument; this module never reads ~/.zcode.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

# ---------------------------------------------------------------------------
# Seam 1: CliAgentAdapter-shaped launch knowledge (spawn family, cf. codex-exec)
# ---------------------------------------------------------------------------

INPUT_TEXT = "text"
COMPLETION_NONE = "none"
COMPLETION_MARKER_PREFIX = "marker:"


@dataclass(frozen=True)
class CliAgentAdapter:
    """Data-driven spawn knowledge: command / prompt_flag / completion / sessions."""

    name: str
    command: Tuple[str, ...]
    input_format: str = INPUT_TEXT
    completion: str = COMPLETION_NONE
    structured_output: bool = False
    supports_stdin_injection: bool = True
    prompt_flag: Optional[str] = None
    session_flag: Optional[str] = None
    resume_flag: Optional[str] = None
    continue_args: Tuple[str, ...] = ()


# ZCode official CLI, headless. Evidence (zcode-app-cli 3.14.3):
#   run.ts:42                  headless prompt default mode = yolo
#   run.ts:115                 OUTPUT_FORMATS = ["text","json","stream-json"]
#   arguments.ts               --resume <id> (resume_flag) / -c short continue
#   prompt-command.ts:328      "stream-json 的 result 是流的终止符"
#   prompt-command.ts:347-359  terminator {"type":"result", sessionId, ...}
ZCODE_ADAPTER = CliAgentAdapter(
    name="zcode",
    command=("zcode", "--mode", "yolo", "--output-format", "stream-json"),
    input_format=INPUT_TEXT,
    # zcode emits its own terminal result event; process exit / stdout EOF also ends turn.
    completion=COMPLETION_MARKER_PREFIX + '"type": "result"',
    structured_output=True,
    supports_stdin_injection=False,  # one-shot: prompt passed as argv flag
    prompt_flag="-p",
    session_flag="--resume",
    # Fix vs the verify script (latent bug there): build_turn_command gates on
    # resume_flag, which the verify script left None — --resume was never emitted
    # and every turn silently ran a fresh session. Here resume_flag carries the
    # value so session continuity actually happens (runbook §2 intent).
    resume_flag="--resume",  # zcode --resume <sessionId>
)


def build_turn_command(
    prompt: str,
    *,
    session_id: Optional[str] = None,
    first_turn: bool = True,
    command_override: Optional[Tuple[str, ...]] = None,
) -> list:
    """Launch argv for one turn; later turns resume the persisted session."""
    argv = list(command_override or ZCODE_ADAPTER.command)
    if session_id and not first_turn and ZCODE_ADAPTER.resume_flag:
        argv += [ZCODE_ADAPTER.resume_flag, session_id]
    argv += [ZCODE_ADAPTER.prompt_flag, prompt]
    return argv


def is_turn_complete(line: str) -> bool:
    """Result-event completion check, parsed structurally (tolerates key spacing)."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        node = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(node, dict) and node.get("type") == "result"


# ---------------------------------------------------------------------------
# Runtime: spawn + parse (standalone, sync)
# ---------------------------------------------------------------------------

MASK_KEYS = ("apikey", "api_key", "token", "authorization", "secret")


def _mask(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<masked>" if isinstance(v, str) and v and any(s in str(k).lower() for s in MASK_KEYS) else _mask(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask(x) for x in obj]
    return obj


@dataclass
class ZcodeRunResult:
    ok: bool
    response: str = ""
    session_id: str = ""
    trace_id: str = ""
    model: str = ""
    provider_id: str = ""
    usage: dict = field(default_factory=dict)
    projection: dict = field(default_factory=dict)
    event_types: list = field(default_factory=list)
    raw_events: list = field(default_factory=list)
    stderr_tail: str = ""
    duration_ms: int = 0
    error: str = ""

    def to_provider_result(self) -> dict:
        """Harness-protocol-shaped projection."""
        return {
            "provider": "zcode",
            "ok": self.ok,
            "result": self.response,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "model": self.model,
            "provider_id": self.provider_id,
            "usage": self.usage,
            "projection": self.projection,
            "metrics": {"duration_ms": self.duration_ms, "event_count": len(self.raw_events)},
            "error": self.error,
        }


def _extract_model(events: list) -> Tuple[str, str]:
    """Best-effort (providerId, modelId) from session/model events."""
    model = provider = ""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        node = ev.get("model")
        if isinstance(node, dict):
            provider = provider or (node.get("providerId") if isinstance(node.get("providerId"), str) else "")
            model = model or (node.get("modelId") if isinstance(node.get("modelId"), str) else "")
        if isinstance(ev.get("modelId"), str):
            model = model or ev["modelId"]
        if isinstance(ev.get("providerId"), str):
            provider = provider or ev["providerId"]
        if provider and model:
            break
    return provider, model


def run_prompt(
    task: str,
    *,
    cwd: Optional[str] = None,
    timeout_s: int = 180,
    session_id: Optional[str] = None,
    first_turn: bool = True,
    log_path: Optional[str] = None,
    zcode_bin: Optional[str] = None,
    runner=None,
) -> ZcodeRunResult:
    """One headless turn through the official zcode CLI; parse the NDJSON stream.

    ``runner`` injects a subprocess.run-alike (tests use it to fake the CLI);
    default is the real ``subprocess.run``.
    """
    argv = build_turn_command(task, session_id=session_id, first_turn=first_turn)
    binary = zcode_bin or shutil.which("zcode")
    if not binary:
        return ZcodeRunResult(ok=False, error="zcode binary not found on PATH")
    argv[0] = binary

    started = time.monotonic()
    result = ZcodeRunResult(ok=False)
    run = runner or subprocess.run
    try:
        proc = run(
            argv, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.error = "timeout after {}s: {}".format(timeout_s, exc)
        return result
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.stderr_tail = (getattr(proc, "stderr", "") or "")[-2000:]

    events = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)

    result.raw_events = events
    result.event_types = [str(e.get("type")) for e in events]

    final = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if final:
        result.ok = proc.returncode == 0
        result.response = final.get("response", "")
        result.session_id = final.get("sessionId", "")
        result.trace_id = final.get("traceId", "")
        result.usage = final.get("usage") or {}
        result.projection = final.get("projection") or {}
    else:
        result.error = result.error or "no result event in stream-json output"
    result.provider_id, result.model = _extract_model(events)

    if log_path:
        payload = {
            # argv logged without the prompt text (task content stays out of logs)
            "argv": [argv[0], ZCODE_ADAPTER.prompt_flag, "<task>",
                     "--mode", "yolo", "--output-format", "stream-json"],
            "task_chars": len(task),
            "returncode": proc.returncode,
            "duration_ms": result.duration_ms,
            "stderr_tail": _mask(result.stderr_tail),
            "events": _mask(events),
        }
        Path(log_path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# Harness-protocol-shaped surface (duck-typed): card + create(config) factory,
# start/stop/send/events/turn_events/abort. The synchronous subset is real; the
# async streaming surface is a documented production work item.
# ---------------------------------------------------------------------------

class ZcodeHarness:
    """Minimal ExternalHarnessProtocol conformance for one-shot zcode turns."""

    card = {
        "id": "zcode",
        "displayName": "ZCode CLI (spawn-family provider)",
        "capabilities": {
            "pause_resume": False,
            "checkpoint": False,
            "steer": False,
            "abort": False,
        },
    }

    def __init__(self, config: Optional[dict] = None) -> None:
        self._config = dict(config or {})
        self.session_id = None  # type: Optional[str]
        self._log = []

    def turn(self, content: str, *, timeout_s: int = 180) -> dict:
        first = self.session_id is None
        res = run_prompt(
            content,
            timeout_s=timeout_s,
            session_id=self.session_id,
            first_turn=first,
            cwd=self._config.get("cwd"),
            log_path=self._config.get("log_path"),
        )
        if res.session_id:
            self.session_id = res.session_id
        self._log.append(res.to_provider_result())
        return res.to_provider_result()

    # -- ExternalHarnessProtocol surface (duck-typed) --
    def start(self, context: Optional[dict] = None) -> None:
        pass

    def stop(self) -> None:
        pass

    def events(self) -> Iterator[dict]:
        return iter(self._log)

    def turn_events(self, turn_id: Optional[str] = None) -> Iterator[dict]:
        return iter(self._log[-1:])

    async def send(self, content, *, mode="auto"):
        raise NotImplementedError("async streaming send: production work item")

    async def abort(self, *, mode: str = "graceful") -> None:
        raise NotImplementedError("one-shot CLI turn: process-level cancel is the abort path")


class ZcodeHarnessProvider:
    """Duck-typed ExternalHarnessProvider: card + create(config)."""

    @property
    def card(self) -> dict:
        return ZcodeHarness.card

    @staticmethod
    def create(config: dict) -> ZcodeHarness:
        return ZcodeHarness(config)

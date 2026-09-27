# coding: utf-8
"""qodercn 薄封装：无 HTTP，仅构造 prompt、调用 CLI、映射 OpenAI 响应。"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

DEFAULT_MODEL = "Qwen3.8-Flash"
DEFAULT_TIMEOUT_S = 180

# qodercn 并发跑会抢共享 CLI 登录态，所有子进程调用必须持有这把锁。
CALL_LOCK = threading.Lock()


@dataclass
class QoderRunResult:
    """一次 qodercn 调用的归一化结果。失败时 ok=False，error 为短原因。"""

    ok: bool = False
    response: str = ""
    subtype: str = ""
    session_id: str = ""
    model: str = ""
    total_credits: Any = None
    total_cost_usd: Any = None
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    duration_ms: Any = None
    returncode: Optional[int] = None
    stderr_tail: str = ""
    error: str = ""

    def to_openai_usage(self) -> Dict[str, int]:
        """usage.input_tokens / output_tokens → OpenAI usage；缺字段按 0。"""
        usage = self.usage if isinstance(self.usage, dict) else {}
        prompt = _as_int(usage.get("input_tokens"))
        completion = _as_int(usage.get("output_tokens"))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _content_text(content: Any) -> str:
    """OpenAI content：字符串原样；多模态列表只拼接 type=text 的 text。"""
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if text is None:
                continue
            parts.append(text if isinstance(text, str) else str(text))
        return "".join(parts)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def build_prompt(messages: Any) -> str:
    """取 messages 里最后一条 role==user 的 content。空列表或没有 user → ValueError。"""
    if not messages:
        raise ValueError("empty messages")
    last = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last = msg
    if last is None:
        raise ValueError("no user message")
    return _content_text(last.get("content"))


def _timeout_label(timeout_s: Any) -> str:
    try:
        number = float(timeout_s)
    except (TypeError, ValueError):
        return str(timeout_s)
    if number == int(number):
        return str(int(number))
    return str(number)


def _parse_last_result(stdout: str) -> Optional[Dict[str, Any]]:
    """最后一条能解析且 type==result 的非空行。夹杂的非 JSON 行忽略。"""
    chosen = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            chosen = obj
    return chosen


def _stderr_tail(stderr: Any) -> str:
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return str(stderr)[-2000:]


def run_qodercn(
    prompt: str,
    *,
    bin_path: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    cwd: Optional[str] = None,
) -> QoderRunResult:
    """唯一的子进程入口。二进制缺失、超时、非 success 都返回 ok=False，不抛给调用方。"""
    binary = bin_path or shutil.which("qodercn")
    if not binary:
        return QoderRunResult(
            ok=False,
            model=model or "",
            error="qodercn binary not found on PATH",
        )

    cmd = [
        binary,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    try:
        with CALL_LOCK:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                cwd=cwd,
            )
    except subprocess.TimeoutExpired as exc:
        return QoderRunResult(
            ok=False,
            model=model or "",
            error="timeout after %ss" % _timeout_label(timeout_s),
            stderr_tail=_stderr_tail(getattr(exc, "stderr", "") or ""),
        )

    stderr_tail = _stderr_tail(getattr(completed, "stderr", "") or "")
    parsed = _parse_last_result(getattr(completed, "stdout", "") or "")
    returncode = getattr(completed, "returncode", None)
    if parsed is None:
        return QoderRunResult(
            ok=False,
            model=model or "",
            raw={},
            returncode=returncode,
            stderr_tail=stderr_tail,
            error="failed to parse qodercn json output",
        )

    subtype_raw = parsed.get("subtype")
    subtype = "" if subtype_raw is None else str(subtype_raw)
    is_error = parsed.get("is_error") is True
    ok = returncode == 0 and subtype == "success" and not is_error

    result = parsed.get("result")
    if result is None:
        response = ""
    elif isinstance(result, str):
        response = result
    else:
        response = str(result)

    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    session_raw = parsed.get("session_id") or ""
    session_id = session_raw if isinstance(session_raw, str) else str(session_raw)

    error = ""
    if not ok:
        reasons = []
        if returncode != 0:
            reasons.append("qodercn exited with code %s" % returncode)
        if subtype != "success":
            reasons.append("subtype %s" % (subtype or "missing"))
        if is_error:
            reasons.append("is_error")
        error = "; ".join(reasons) if reasons else "qodercn failed"

    return QoderRunResult(
        ok=ok,
        response=response,
        subtype=subtype,
        session_id=session_id,
        model=model or "",
        total_credits=parsed.get("total_credits"),
        total_cost_usd=parsed.get("total_cost_usd"),
        usage=usage,
        raw=parsed,
        duration_ms=parsed.get("duration_ms"),
        returncode=returncode,
        stderr_tail=stderr_tail,
        error=error,
    )


def run_prompt(messages: Any, **kwargs: Any) -> QoderRunResult:
    """build_prompt + run_qodercn。kwargs 原样传给 run_qodercn（model/timeout_s/bin_path/cwd）。"""
    prompt = build_prompt(messages)
    return run_qodercn(prompt, **kwargs)


def to_chat_completion(res: QoderRunResult, request_model: str) -> Dict[str, Any]:
    """QoderRunResult → OpenAI chat.completion。扩展字段放在 qoder 下，不改 choices 形状。"""
    finish = "stop" if res.subtype == "success" else "error"
    return {
        "id": "chatcmpl-qoder-%s" % uuid.uuid4().hex[:12],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": res.response},
                "finish_reason": finish,
            }
        ],
        "usage": res.to_openai_usage(),
        "qoder": {
            "subtype": res.subtype,
            "session_id": res.session_id,
            "total_credits": res.total_credits,
            "total_cost_usd": res.total_cost_usd,
            "duration_ms": res.duration_ms,
        },
    }


def error_body(message: str, err_type: str, code: str) -> Dict[str, Any]:
    return {"error": {"message": message, "type": err_type, "code": code}}

# coding: utf-8
"""qodercn 的 OpenAI 兼容本地 HTTP 服务（仅标准库）。

jiuwenswarm 用 B-1 零侵入接法指向本服务：
``model_client_config.api_base = http://127.0.0.1:8123/v1``，``client_provider: OpenAI``。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

try:
    import provider as qoder_provider
except ImportError:  # 以包形式加载时走相对导入
    from . import provider as qoder_provider


_KNOWN_PATHS = ("/healthz", "/v1/models", "/v1/chat/completions")
Runner = Callable[[Any, str], Any]


def _default_log_dir() -> Path:
    return Path(__file__).resolve().parent / "logs"


def _is_binary_missing(error: str) -> bool:
    text = (error or "").lower().replace("_", " ").replace("-", " ")
    return "binary" in text and "not found" in text


def _prompt_chars(messages: Any) -> int:
    try:
        return len(qoder_provider.build_prompt(messages))
    except (TypeError, ValueError):
        return 0


def _has_user_message(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            return True
    return False


def _append_log(log_dir: Path, record: Dict[str, Any]) -> None:
    """请求日志失败不得影响响应。只写计量字段，不写 prompt / argv / message。"""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "requests.jsonl"
        line = json.dumps(record, ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
    except Exception:
        traceback.print_exc(file=sys.stderr)


def _log_record(
    remote: str,
    model: str,
    prompt_chars: int,
    status: int,
    subtype: str,
    total_credits: Any,
    total_cost_usd: Any,
    duration_ms: Any,
    error: str,
) -> Dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "remote": remote,
        "model": model or "",
        "prompt_chars": prompt_chars,
        "status": status,
        "subtype": subtype or "",
        "total_credits": total_credits,
        "total_cost_usd": total_cost_usd,
        "duration_ms": duration_ms,
        "error": error or "",
    }


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(
    host: str,
    port: int,
    runner: Optional[Runner] = None,
    log_dir: Optional[Any] = None,
    model: Optional[str] = None,
    timeout_s: Optional[float] = None,
    bin_path: Optional[str] = None,
):
    """测试入口。runner(messages, model) 替换 provider.run_prompt。"""
    cfg_model = model or qoder_provider.DEFAULT_MODEL
    cfg_timeout = qoder_provider.DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    cfg_bin = bin_path or None
    cfg_log = Path(log_dir) if log_dir is not None else _default_log_dir()

    class Handler(BaseHTTPRequestHandler):
        server_version = "qoder-provider/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def _respond(self, status: int, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _remote(self) -> str:
            address = self.client_address
            if isinstance(address, tuple) and address:
                return str(address[0])
            return ""

        def _invoke(self, messages: Any, req_model: str) -> Any:
            if runner is not None:
                return runner(messages, req_model)
            return qoder_provider.run_prompt(
                messages,
                model=req_model,
                timeout_s=cfg_timeout,
                bin_path=cfg_bin,
            )

        def _chat(self, remote: str) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
            raw = self._read_body()
            if not raw:
                payload = qoder_provider.error_body(
                    "missing messages", "invalid_request_error", "missing_messages",
                )
                record = _log_record(remote, cfg_model, 0, 400, "", None, None, None, "missing messages")
                return 400, payload, record
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = qoder_provider.error_body(
                    "request body is not valid JSON", "invalid_request_error", "bad_json",
                )
                record = _log_record(remote, "", 0, 400, "", None, None, None, "bad json")
                return 400, payload, record

            if not isinstance(body, dict) or "messages" not in body:
                payload = qoder_provider.error_body(
                    "missing messages", "invalid_request_error", "missing_messages",
                )
                record = _log_record(remote, cfg_model, 0, 400, "", None, None, None, "missing messages")
                return 400, payload, record

            messages = body.get("messages")
            if not isinstance(messages, list) or not messages or not _has_user_message(messages):
                if not isinstance(messages, list) or not messages:
                    message = "missing messages"
                    code = "missing_messages"
                else:
                    message = "no user message"
                    code = "missing_user_message"
                payload = qoder_provider.error_body(message, "invalid_request_error", code)
                record = _log_record(remote, cfg_model, 0, 400, "", None, None, None, message)
                return 400, payload, record

            req_model = body.get("model") or cfg_model
            if not isinstance(req_model, str):
                req_model = str(req_model)
            chars = _prompt_chars(messages)
            try:
                res = self._invoke(messages, req_model)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                payload = qoder_provider.error_body(
                    "internal error", "internal_error", "internal_error",
                )
                record = _log_record(
                    remote, req_model, chars, 500, "", None, None, None, "internal error",
                )
                return 500, payload, record

            if getattr(res, "ok", False):
                payload = qoder_provider.to_chat_completion(res, req_model)
                record = _log_record(
                    remote,
                    req_model,
                    chars,
                    200,
                    getattr(res, "subtype", "") or "",
                    getattr(res, "total_credits", None),
                    getattr(res, "total_cost_usd", None),
                    getattr(res, "duration_ms", None),
                    "",
                )
                return 200, payload, record

            err = getattr(res, "error", "") or ""
            subtype = getattr(res, "subtype", "") or ""
            if _is_binary_missing(err):
                status = 500
                payload = qoder_provider.error_body(
                    err or "qodercn binary not found",
                    "internal_error",
                    "provider_binary_missing",
                )
            else:
                status = 502
                payload = qoder_provider.error_body(
                    err or "qodercn failed",
                    "upstream_error",
                    "qodercn_failed",
                )
            record = _log_record(
                remote,
                req_model,
                chars,
                status,
                subtype,
                getattr(res, "total_credits", None),
                getattr(res, "total_cost_usd", None),
                getattr(res, "duration_ms", None),
                err,
            )
            return status, payload, record

        def _dispatch(self, method: str) -> Tuple[int, Dict[str, Any], Optional[Dict[str, Any]]]:
            path = urlparse(self.path).path
            remote = self._remote()
            if path not in _KNOWN_PATHS:
                payload = qoder_provider.error_body(
                    "unknown path: %s" % path, "invalid_request_error", "not_found",
                )
                return 404, payload, None
            if path == "/healthz":
                if method != "GET":
                    payload = qoder_provider.error_body(
                        "method not allowed", "invalid_request_error", "method_not_allowed",
                    )
                    return 405, payload, None
                payload = {"status": "ok", "provider": "qoder", "model": cfg_model}
                return 200, payload, None
            if path == "/v1/models":
                if method != "GET":
                    payload = qoder_provider.error_body(
                        "method not allowed", "invalid_request_error", "method_not_allowed",
                    )
                    return 405, payload, None
                payload = {
                    "object": "list",
                    "data": [
                        {
                            "id": cfg_model,
                            "object": "model",
                            "created": int(datetime.now(timezone.utc).timestamp()),
                            "owned_by": "qoder",
                        }
                    ],
                }
                return 200, payload, None
            if method != "POST":
                payload = qoder_provider.error_body(
                    "method not allowed", "invalid_request_error", "method_not_allowed",
                )
                return 405, payload, None
            status, payload, record = self._chat(remote)
            return status, payload, record

        def _handle(self, method: str) -> None:
            log_record = None
            try:
                status, payload, log_record = self._dispatch(method)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                status = 500
                payload = qoder_provider.error_body(
                    "internal error", "internal_error", "internal_error",
                )
            if log_record is not None:
                _append_log(cfg_log, log_record)
            try:
                self._respond(status, payload)
            except Exception:
                traceback.print_exc(file=sys.stderr)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle("PATCH")

    return _ThreadingServer((host, port), Handler)


def _env_timeout() -> float:
    raw = os.environ.get("QODER_TIMEOUT_S")
    if raw is None or raw == "":
        return qoder_provider.DEFAULT_TIMEOUT_S
    number = float(raw)
    if number == int(number):
        return int(number)
    return number


def main() -> None:
    host = os.environ.get("QODER_PROVIDER_HOST") or "127.0.0.1"
    port = int(os.environ.get("QODER_PROVIDER_PORT") or "8123")
    model = os.environ.get("QODER_MODEL") or qoder_provider.DEFAULT_MODEL
    timeout_s = _env_timeout()
    bin_path = os.environ.get("QODER_BIN") or None
    log_env = os.environ.get("QODER_LOG_DIR")
    log_dir = Path(log_env) if log_env else _default_log_dir()
    server = make_server(
        host,
        port,
        runner=None,
        log_dir=log_dir,
        model=model,
        timeout_s=timeout_s,
        bin_path=bin_path,
    )
    print("qoder provider listening on http://%s:%s/v1" % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

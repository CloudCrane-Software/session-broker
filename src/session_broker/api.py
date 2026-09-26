# coding: utf-8
"""最小 REST API（纯标准库 http.server）.

路由（MVP 治理壳——无任何真实 OAuth 会话接入）::

    GET  /health                       → 200 {"status":"ok","redline":...}
    GET  /sessions?state=ACTIVE        → 200 {"sessions":[...]}
    POST /sessions                     → 201 {"session":...}   登记新会话
    POST /sessions/{id}/activate       → 200 / 403(红线拒绝) / 404
    POST /sessions/{id}/revoke         → 200（幂等）
    POST /reviews                      → 201 登记 ToS 评审记录（红线门的输入）

错误统一 JSON：``{"error": {"code": ..., "message": ...}}``。
请求日志只记 **字段名**（body_fields=...），绝不记值。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .audit import AuditLog
from .policy import (
    REDLINE_TEXT,
    PolicyDenied,
    ReviewStore,
    check_activation,
    parse_review_date,
)
from .registry import SessionRegistry, session_dict

__all__ = ["BrokerApp", "make_server"]

_LOG = logging.getLogger("session_broker.api")


def _default_clock():
    return datetime.now(timezone.utc)


def _iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class BrokerApp:
    """把 registry/policy/audit 装配为一个可独立测试的应用层（dispatch 为纯函数）。"""

    def __init__(self, clock=None):
        self._clock = clock or _default_clock
        self.audit = AuditLog(clock=self._clock)
        self.reviews = ReviewStore()
        self.registry = SessionRegistry(gate=self._gate, audit=self.audit,
                                        clock=self._clock)

    def _gate(self, session):
        return check_activation(session, self.reviews, self._clock())

    # ------------------------------------------------------------------ 路由

    def dispatch(self, method, path, query=None, body=None):
        """返回 (status, payload)。任何分支不抛异常（应用层内聚错误→JSON）。"""
        query = dict(query or {})
        try:
            if path == "/health":
                return self._health(method)
            if path == "/sessions":
                if method == "GET":
                    return self._list_sessions(query)
                if method == "POST":
                    return self._create_session(body)
                return self._err(405, "METHOD_NOT_ALLOWED", "%s /sessions" % method)
            if path == "/reviews":
                if method == "POST":
                    return self._register_review(body)
                return self._err(405, "METHOD_NOT_ALLOWED", "%s /reviews" % method)
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] in ("activate", "revoke"):
                if method != "POST":
                    return self._err(405, "METHOD_NOT_ALLOWED", "%s %s" % (method, path))
                if parts[2] == "activate":
                    return self._activate(parts[1], body)
                return self._revoke(parts[1], body)
            return self._err(404, "NOT_FOUND", "unknown route: %s" % path)
        except KeyError:
            return self._err(404, "SESSION_NOT_FOUND", "no such session")
        except ValueError as exc:
            return self._err(400, "INVALID_FIELD", str(exc))

    # ------------------------------------------------------------------ 分支

    def _health(self, method):
        if method != "GET":
            return self._err(405, "METHOD_NOT_ALLOWED", "%s /health" % method)
        return 200, {"status": "ok", "redline": REDLINE_TEXT, "time": _iso(self._clock())}

    def _list_sessions(self, query):
        state = query.get("state")
        if state is not None and state not in ("REGISTERED", "ACTIVE", "REVOKED", "EXPIRED"):
            return self._err(400, "INVALID_STATE_FILTER",
                             "state must be one of REGISTERED|ACTIVE|REVOKED|EXPIRED")
        sessions = [session_dict(s) for s in self.registry.list(state=state)]
        return 200, {"sessions": sessions}

    def _create_session(self, body):
        body = body if isinstance(body, dict) else {}
        missing = [f for f in ("vendor", "subject_ref", "login_state_ref")
                   if not isinstance(body.get(f), str) or not body.get(f).strip()]
        if missing:
            return self._err(400, "MISSING_FIELD", "missing/empty fields: %s"
                             % ", ".join(missing))
        session = self.registry.register(
            vendor=body["vendor"],
            subject_ref=body["subject_ref"],
            login_state_ref=body["login_state_ref"],
            review_ref=body.get("review_ref") or "",
        )
        return 201, {"session": session_dict(session)}

    def _activate(self, session_id, body):
        body = body if isinstance(body, dict) else {}
        try:
            session = self.registry.activate(session_id,
                                             actor=str(body.get("actor") or "api"))
        except PolicyDenied as exc:
            return self._err(403, exc.code, exc.message)
        return 200, {"session": session_dict(session)}

    def _revoke(self, session_id, body):
        body = body if isinstance(body, dict) else {}
        session = self.registry.revoke(session_id,
                                       actor=str(body.get("actor") or "api"),
                                       reason=str(body.get("reason") or ""))
        return 200, {"session": session_dict(session)}

    def _register_review(self, body):
        body = body if isinstance(body, dict) else {}
        missing = [f for f in ("review_ref", "vendor", "reviewed_at")
                   if not isinstance(body.get(f), str) or not body.get(f).strip()]
        if missing:
            return self._err(400, "MISSING_FIELD", "missing/empty fields: %s"
                             % ", ".join(missing))
        record = self.reviews.register(
            review_ref=body["review_ref"],
            vendor=body["vendor"],
            reviewed_at=body["reviewed_at"],
            decision=str(body.get("decision") or "approved"),
            tos_version=str(body.get("tos_version") or ""),
        )
        return 201, {"review": {
            "review_ref": record.review_ref,
            "vendor": record.vendor,
            "reviewed_at": record.reviewed_at,
            "decision": record.decision,
            "tos_version": record.tos_version,
        }}

    @staticmethod
    def _err(status, code, message):
        return status, {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------- HTTP

def make_server(app, host="127.0.0.1", port=0):
    """把 BrokerApp 挂到 ThreadingHTTPServer（测试默认 127.0.0.1:0 临时端口）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "session-broker/0.1"

        def log_message(self, fmt, *args):  # noqa: A003 — 静默默认访问日志
            pass

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return _BAD_JSON

        def _respond(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = self._body()
            if body is _BAD_JSON:
                # 红线纪律：日志只记字段名，不记值；坏 JSON 无字段名可言
                _LOG.warning("%s %s body=<unparseable>", method, parsed.path)
                self._respond(400, {"error": {"code": "BAD_JSON",
                                              "message": "request body is not valid JSON"}})
                return
            fields = ",".join(sorted(body)) if isinstance(body, dict) else "-"
            _LOG.info("%s %s body_fields=%s", method, parsed.path, fields)
            status, payload = app.dispatch(method, parsed.path, query=query, body=body)
            self._respond(status, payload)

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)


_BAD_JSON = object()

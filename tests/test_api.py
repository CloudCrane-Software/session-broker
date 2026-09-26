# coding: utf-8
"""REST API 测试：dispatch 纯函数分支 + 真实端口端到端 + 请求日志只记字段名."""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

import pytest

from session_broker import REDLINE_TEXT
from session_broker.api import BrokerApp, make_server

LOGIN_REF = "openbao://secret/sessions/grok/user1"
FRESH_DATE = "2026-09-25"          # FROZEN_NOW(2026-09-26) 前一天


@pytest.fixture
def app():
    from conftest import FakeClock
    return BrokerApp(clock=FakeClock())


def call(app, method, path, body=None, query=None):
    return app.dispatch(method, path, query=query, body=body)


def register_session(app, review_ref=""):
    status, payload = call(app, "POST", "/sessions", body={
        "vendor": "grok", "subject_ref": "user1",
        "login_state_ref": LOGIN_REF, "review_ref": review_ref,
    })
    assert status == 201
    return payload["session"]


# ---------------------------------------------------------------- 基础路由

def test_health(app):
    status, payload = call(app, "GET", "/health")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["redline"] == REDLINE_TEXT


def test_health_rejects_post(app):
    status, payload = call(app, "POST", "/health", body={})
    assert status == 405 and payload["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unknown_route_404(app):
    status, payload = call(app, "GET", "/nope")
    assert status == 404 and payload["error"]["code"] == "NOT_FOUND"


def test_error_body_is_json_shape(app):
    _, payload = call(app, "GET", "/nope")
    assert set(payload["error"]) == {"code", "message"}


# ---------------------------------------------------------------- 会话 CRUD

def test_register_and_list(app):
    sess = register_session(app)
    assert sess["state"] == "REGISTERED"
    status, payload = call(app, "GET", "/sessions")
    assert status == 200 and len(payload["sessions"]) == 1
    assert payload["sessions"][0]["session_id"] == sess["session_id"]
    status, payload = call(app, "GET", "/sessions", query={"state": "ACTIVE"})
    assert payload["sessions"] == []


def test_register_missing_fields_400(app):
    status, payload = call(app, "POST", "/sessions", body={"vendor": "grok"})
    assert status == 400
    assert payload["error"]["code"] == "MISSING_FIELD"
    assert "subject_ref" in payload["error"]["message"]
    assert "login_state_ref" in payload["error"]["message"]


def test_register_raw_credential_400(app):
    status, payload = call(app, "POST", "/sessions", body={
        "vendor": "grok", "subject_ref": "user1",
        "login_state_ref": "raw-token-value-1234567890",
    })
    assert status == 400 and payload["error"]["code"] == "INVALID_FIELD"


def test_activate_without_review_403(app):
    sess = register_session(app)
    status, payload = call(app, "POST", "/sessions/%s/activate" % sess["session_id"],
                           body={"actor": "tester"})
    assert status == 403
    assert payload["error"]["code"] == "REVIEW_REQUIRED"
    assert REDLINE_TEXT in payload["error"]["message"]


def test_full_activate_flow_via_api(app):
    status, _ = call(app, "POST", "/reviews", body={
        "review_ref": "tos-review/grok/001", "vendor": "grok",
        "reviewed_at": FRESH_DATE, "tos_version": "v2026.1",
    })
    assert status == 201
    sess = register_session(app, review_ref="tos-review/grok/001")
    status, payload = call(app, "POST",
                           "/sessions/%s/activate" % sess["session_id"], body={})
    assert status == 200
    assert payload["session"]["state"] == "ACTIVE"
    assert payload["session"]["activated_at"] == "2026-09-26T12:00:00+00:00"


def test_activate_unknown_session_404(app):
    status, payload = call(app, "POST", "/sessions/sess-nope/activate", body={})
    assert status == 404 and payload["error"]["code"] == "SESSION_NOT_FOUND"


def test_revoke_via_api_idempotent(app):
    call(app, "POST", "/reviews", body={
        "review_ref": "tos-review/grok/001", "vendor": "grok",
        "reviewed_at": FRESH_DATE,
    })
    sess = register_session(app, review_ref="tos-review/grok/001")
    call(app, "POST", "/sessions/%s/activate" % sess["session_id"], body={})
    status, payload = call(app, "POST", "/sessions/%s/revoke" % sess["session_id"],
                           body={"reason": "tos changed"})
    assert status == 200 and payload["session"]["state"] == "REVOKED"
    status, payload = call(app, "POST", "/sessions/%s/revoke" % sess["session_id"],
                           body={"reason": "again"})
    assert status == 200 and payload["session"]["state"] == "REVOKED"
    assert len(app.audit.by_type("revoke")) == 1


def test_review_bad_date_400(app):
    status, payload = call(app, "POST", "/reviews", body={
        "review_ref": "r", "vendor": "grok", "reviewed_at": "09/26/2026",
    })
    assert status == 400 and payload["error"]["code"] == "INVALID_FIELD"


def test_state_filter_bad_value_400(app):
    status, _ = call(app, "GET", "/sessions", query={"state": "YESTERDAY"})
    assert status == 400


# ---------------------------------------------------------------- HTTP 实测

@pytest.fixture
def http_app():
    from conftest import FakeClock
    app = BrokerApp(clock=FakeClock())
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def test_http_end_to_end(http_app):
    app, base = http_app
    with urllib.request.urlopen(base + "/health", timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read().decode())["status"] == "ok"

    req = urllib.request.Request(
        base + "/sessions", method="POST",
        data=json.dumps({"vendor": "grok", "subject_ref": "user1",
                         "login_state_ref": LOGIN_REF}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 201
        sid = json.loads(resp.read().decode())["session"]["session_id"]

    try:
        urllib.request.urlopen(base + "/sessions/%s/activate" % sid, data=b"{}",
                               timeout=5)
        raise AssertionError("expected 403")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
        body = json.loads(exc.read().decode())
        assert body["error"]["code"] == "REVIEW_REQUIRED"


def test_request_log_records_field_names_not_values(http_app, caplog):
    app, base = http_app
    secret_marker = "SUPER-SECRET-VALUE-XYZZY"
    req = urllib.request.Request(
        base + "/sessions", method="POST",
        data=json.dumps({"vendor": "grok", "subject_ref": secret_marker,
                         "login_state_ref": LOGIN_REF}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with caplog.at_level(logging.INFO, logger="session_broker.api"):
        urllib.request.urlopen(req, timeout=5)
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "subject_ref" in log_text            # 字段名在
    assert secret_marker not in log_text        # 值永不在
    assert LOGIN_REF not in log_text

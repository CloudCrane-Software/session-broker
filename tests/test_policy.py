# coding: utf-8
"""红线门与登记册测试（无评审拒 / 评审过期拒 / 审计在 / 幂等 / 原始凭据拒收）."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from session_broker import (
    REDLINE_TEXT,
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_REGISTERED,
    STATE_REVOKED,
    AuditLog,
    PolicyDenied,
    ReviewStore,
    SessionRegistry,
    check_activation,
)
from conftest import FROZEN_NOW, FakeClock

LOGIN_REF = "openbao://secret/sessions/grok/user1"


def days_ago(n):
    return (FROZEN_NOW - timedelta(days=n)).date().isoformat()


@pytest.fixture
def broker():
    clock = FakeClock()
    audit = AuditLog(clock=clock)
    reviews = ReviewStore()
    registry = SessionRegistry(
        gate=lambda s: check_activation(s, reviews, clock()),
        audit=audit, clock=clock,
    )
    return SimpleNamespace(registry=registry, reviews=reviews, audit=audit,
                           clock=clock)


def make_review(broker, review_ref="tos-review/grok/001", vendor="grok",
                reviewed_at=None, decision="approved"):
    return broker.reviews.register(
        review_ref=review_ref, vendor=vendor,
        reviewed_at=reviewed_at or days_ago(1),
        decision=decision, tos_version="v2026.1",
    )


def make_session(broker, review_ref="tos-review/grok/001", vendor="grok"):
    return broker.registry.register(
        vendor=vendor, subject_ref="user1",
        login_state_ref=LOGIN_REF.replace("grok", vendor),
        review_ref=review_ref,
    )


# ---------------------------------------------------------------- 红线常量

def test_redline_text_exact():
    assert REDLINE_TEXT == "不得违反订阅条款使用 OAuth 会话"


def test_redline_text_in_readme_and_docs():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    assert REDLINE_TEXT in (root / "README.md").read_text(encoding="utf-8")
    assert REDLINE_TEXT in (root / "docs" / "relay-design.md").read_text(encoding="utf-8")
    assert REDLINE_TEXT in (root / "src" / "session_broker" / "policy.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 红线门：拒绝路径

def test_activate_without_review_ref_denied(broker):
    sess = make_session(broker, review_ref="")
    with pytest.raises(PolicyDenied) as ei:
        broker.registry.activate(sess.session_id, actor="tester")
    assert ei.value.code == "REVIEW_REQUIRED"
    assert sess.state == STATE_REGISTERED          # 状态停在 REGISTERED
    denies = broker.audit.by_type("deny")
    assert len(denies) == 1
    assert REDLINE_TEXT in denies[0]["reason"]     # 拒绝理由携带红线文本
    assert denies[0]["actor"] == "tester"


def test_activate_without_review_record_denied(broker):
    sess = make_session(broker)                    # 有 review_ref，但无评审记录
    with pytest.raises(PolicyDenied) as ei:
        broker.registry.activate(sess.session_id)
    assert ei.value.code == "REVIEW_NOT_FOUND"
    assert sess.state == STATE_REGISTERED          # vendor 无 ToS 评审记录 → 停在 REGISTERED
    assert len(broker.audit.by_type("deny")) == 1


def test_review_expired_91_days_denied(broker):
    make_review(broker, reviewed_at=days_ago(91))
    sess = make_session(broker)
    with pytest.raises(PolicyDenied) as ei:
        broker.registry.activate(sess.session_id)
    assert ei.value.code == "REVIEW_EXPIRED"
    assert sess.state == STATE_REGISTERED


def test_review_boundary_90_days_allowed(broker):
    make_review(broker, reviewed_at=days_ago(90))  # 恰好 90 天：仍有效
    sess = make_session(broker)
    broker.registry.activate(sess.session_id)
    assert sess.state == STATE_ACTIVE


def test_review_not_approved_denied(broker):
    make_review(broker, decision="rejected")
    sess = make_session(broker)
    with pytest.raises(PolicyDenied) as ei:
        broker.registry.activate(sess.session_id)
    assert ei.value.code == "REVIEW_NOT_APPROVED"
    assert sess.state == STATE_REGISTERED


def test_vendor_mismatch_denied(broker):
    broker.reviews.register(review_ref="tos-review/other/001", vendor="other",
                            reviewed_at=days_ago(1))
    sess = make_session(broker, review_ref="tos-review/other/001")
    with pytest.raises(PolicyDenied) as ei:
        broker.registry.activate(sess.session_id)
    assert ei.value.code == "VENDOR_MISMATCH"


# ---------------------------------------------------------------- 激活 / 撤销 / 到期

def test_review_fresh_activates_and_audits(broker):
    make_review(broker, reviewed_at=days_ago(1))
    sess = make_session(broker)
    broker.registry.activate(sess.session_id, actor="tester")
    assert sess.state == STATE_ACTIVE
    assert sess.activated_at == FROZEN_NOW.isoformat()
    assert sess.expires_at == (FROZEN_NOW + timedelta(hours=8)).isoformat()
    events = broker.audit.by_type("activate")
    assert len(events) == 1 and events[0]["actor"] == "tester"


def test_activate_idempotent_no_duplicate_audit(broker):
    make_review(broker)
    sess = make_session(broker)
    broker.registry.activate(sess.session_id)
    again = broker.registry.activate(sess.session_id)
    assert again is sess and sess.state == STATE_ACTIVE
    assert len(broker.audit.by_type("activate")) == 1


def test_activate_after_revoke_denied(broker):
    make_review(broker)
    sess = make_session(broker)
    broker.registry.activate(sess.session_id)
    broker.registry.revoke(sess.session_id, reason="tos changed")
    with pytest.raises(PolicyDenied) as ei:
        broker.registry.activate(sess.session_id)
    assert ei.value.code == "STATE_NOT_ACTIVATABLE"


def test_revoke_idempotent_no_duplicate_audit(broker):
    make_review(broker)
    sess = make_session(broker)
    broker.registry.activate(sess.session_id)
    broker.registry.revoke(sess.session_id, reason="first")
    assert broker.registry.revoke(sess.session_id, reason="second").state == STATE_REVOKED
    revokes = broker.audit.by_type("revoke")
    assert len(revokes) == 1
    assert revokes[0]["reason"] == "first"
    assert revokes[0]["prev_state"] == STATE_ACTIVE


def test_revoke_registered_session_allowed(broker):
    sess = make_session(broker, review_ref="")
    broker.registry.revoke(sess.session_id, reason="cleanup")
    assert sess.state == STATE_REVOKED


def test_sweep_expired_marks_and_audits(broker):
    clock = FakeClock()
    reviews = ReviewStore()
    reviews.register(review_ref="r1", vendor="grok", reviewed_at=days_ago(1))
    registry = SessionRegistry(
        gate=lambda s: check_activation(s, reviews, clock()),
        audit=AuditLog(clock=clock), clock=clock, session_ttl_hours=1,
    )
    sess = registry.register("grok", "user1", LOGIN_REF, review_ref="r1")
    registry.activate(sess.session_id)
    assert registry.sweep_expired() == []          # 未到期
    clock.advance(hours=2)
    expired = registry.sweep_expired()
    assert [s.session_id for s in expired] == [sess.session_id]
    assert sess.state == STATE_EXPIRED
    # 到期后激活被拒
    with pytest.raises(PolicyDenied):
        registry.activate(sess.session_id)


# ---------------------------------------------------------------- 登记册卫生

def test_register_rejects_raw_credential_as_login_state_ref(broker):
    with pytest.raises(ValueError, match="key-vault URI"):
        broker.registry.register("grok", "user1", "raw-token-value-1234567890abcdef")


def test_register_rejects_empty_fields(broker):
    for kwargs in ({"vendor": ""}, {"subject_ref": " "},
                   {"vendor": "grok", "subject_ref": "u", "login_state_ref": ""}):
        base = {"vendor": "grok", "subject_ref": "user1", "login_state_ref": LOGIN_REF}
        base.update(kwargs)
        with pytest.raises(ValueError):
            broker.registry.register(**base)


def test_audit_events_carry_refs_not_values(broker):
    make_review(broker)
    make_session(broker)
    sess2 = make_session(broker, review_ref="tos-review/grok/missing")
    with pytest.raises(PolicyDenied):
        broker.registry.activate(sess2.session_id)
    dumped = repr(broker.audit.events())
    assert "openbao://secret/sessions/grok/user1" not in dumped  # 登录态引用本身也不该乱进事件
    assert sess2.session_id in dumped                            # subject 是 session_id（引用）


def test_list_filter_by_state(broker):
    make_review(broker)
    s1 = make_session(broker)
    s2 = broker.registry.register("grok", "user2",
                                  "openbao://secret/sessions/grok/user2")
    broker.registry.activate(s1.session_id)
    assert [s.session_id for s in broker.registry.list(state=STATE_ACTIVE)] == [s1.session_id]
    assert len(broker.registry.list(state=STATE_REGISTERED)) == 1
    assert len(broker.registry.list()) == 2
    assert s2.state == STATE_REGISTERED


def test_unknown_session_keyerror(broker):
    with pytest.raises(KeyError):
        broker.registry.activate("sess-nope")
    with pytest.raises(KeyError):
        broker.registry.revoke("sess-nope")

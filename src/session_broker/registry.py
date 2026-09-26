# coding: utf-8
"""厂商会话登记册（C 路线治理壳——只登记与治理，不做任何真实 OAuth 接入）.

状态机::

    REGISTERED ──activate(过红线门)──▶ ACTIVE ──revoke──▶ REVOKED
        │                                │  ▲
        │ revoke                         │  sweep_expired（expires_at 到期）
        ▼                                ▼  │
     REVOKED                          EXPIRED（终态，同样可 revoke 留痕为幂等无操作）

- ``login_state_ref`` 指向**密钥库路径**（URI 形式，如 ``openbao://secret/sessions/<vendor>/…``），
  登记册任何字段都不得出现登录态**值**；
- ``review_ref`` 必填才能激活（红线门在 :mod:`session_broker.policy`）；
- activate/revoke 幂等：对已处于目标状态的会话重复操作 = 无状态变化、无重复审计。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .audit import AuditLog
from .policy import DEFAULT_SESSION_TTL_HOURS, GateResult, PolicyDenied

__all__ = [
    "STATE_REGISTERED", "STATE_ACTIVE", "STATE_REVOKED", "STATE_EXPIRED",
    "VendorSession", "SessionRegistry", "session_dict",
]

STATE_REGISTERED = "REGISTERED"
STATE_ACTIVE = "ACTIVE"
STATE_REVOKED = "REVOKED"
STATE_EXPIRED = "EXPIRED"

# login_state_ref 必须是"引用"（URI 形式），裸值会被拒收——红线的技术兜底之一
_REF_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+$", re.IGNORECASE)


def _default_clock():
    return datetime.now(timezone.utc)


@dataclass
class VendorSession:
    session_id: str
    vendor: str
    subject_ref: str
    login_state_ref: str                     # 指向密钥库路径，而非值
    review_ref: str = ""                     # ToS 评审记录引用；激活硬门槛
    state: str = STATE_REGISTERED
    registered_at: str = ""
    activated_at: str = ""
    expires_at: str = ""
    extra: dict = field(default_factory=dict)


def session_dict(session):
    """会话 → 可序列化 dict（全部字段均为引用/状态/时间，无敏感值）。"""
    return {
        "session_id": session.session_id,
        "vendor": session.vendor,
        "subject_ref": session.subject_ref,
        "login_state_ref": session.login_state_ref,
        "review_ref": session.review_ref,
        "state": session.state,
        "registered_at": session.registered_at,
        "activated_at": session.activated_at,
        "expires_at": session.expires_at,
    }


class SessionRegistry:
    """内存登记册 + 红线门编排。注入 ``gate``（policy.check_activation 的闭包）与审计。"""

    def __init__(self, gate, audit=None, clock=None, session_ttl_hours=None):
        self._gate = gate
        # 注意：AuditLog 实现了 __len__，空日志为 falsy——必须显式判 None
        self._audit = audit if audit is not None else AuditLog(clock=clock)
        self._clock = clock or _default_clock
        self._ttl_hours = DEFAULT_SESSION_TTL_HOURS if session_ttl_hours is None \
            else session_ttl_hours
        self._sessions = {}

    # ---------------------------------------------------------------- 查询

    def get(self, session_id):
        return self._sessions.get(session_id)

    def list(self, state=None):
        sessions = self._sessions.values()
        if state is not None:
            sessions = [s for s in sessions if s.state == state]
        return sorted(sessions, key=lambda s: s.session_id)

    def __len__(self):
        return len(self._sessions)

    # ---------------------------------------------------------------- 登记

    def register(self, vendor, subject_ref, login_state_ref, review_ref="",
                 session_id=None):
        for name, val in (("vendor", vendor), ("subject_ref", subject_ref),
                          ("login_state_ref", login_state_ref)):
            if not isinstance(val, str) or not val.strip():
                raise ValueError("%s must be a non-empty string" % name)
        if not _REF_RE.match(login_state_ref):
            raise ValueError(
                "login_state_ref must be a key-vault URI reference "
                "(e.g. 'openbao://secret/sessions/<vendor>/<subject>'), "
                "never a raw credential"
            )
        if review_ref and not isinstance(review_ref, str):
            raise ValueError("review_ref must be a string")
        sid = session_id or "sess-%s-%s" % (vendor, uuid.uuid4().hex[:8])
        if sid in self._sessions:
            raise ValueError("duplicate session_id: %s" % sid)
        now = self._clock()
        session = VendorSession(
            session_id=sid,
            vendor=vendor,
            subject_ref=subject_ref,
            login_state_ref=login_state_ref,
            review_ref=review_ref or "",
            state=STATE_REGISTERED,
            registered_at=now.isoformat(),
        )
        self._sessions[sid] = session
        self._audit.append("register", actor="registry", subject=sid,
                           reason="session registered (state=REGISTERED)",
                           vendor=vendor, review_ref=session.review_ref)
        return session

    # ---------------------------------------------------------------- 激活

    def activate(self, session_id, actor="registry"):
        session = self._require(session_id)
        if session.state == STATE_ACTIVE:
            return session                      # 幂等：无状态变化、无重复审计
        if session.state in (STATE_REVOKED, STATE_EXPIRED):
            raise PolicyDenied(
                "STATE_NOT_ACTIVATABLE",
                "session %s is %s and cannot be activated" % (session_id, session.state),
            )
        result = self._gate(session)
        if not isinstance(result, GateResult):
            raise TypeError("gate must return GateResult")
        if not result.allowed:
            self._audit.append("deny", actor=actor, subject=session_id,
                               reason="%s: %s" % (result.code, result.message),
                               vendor=session.vendor)
            raise PolicyDenied(result.code, result.message)
        now = self._clock()
        session.state = STATE_ACTIVE
        session.activated_at = now.isoformat()
        session.expires_at = (now + timedelta(hours=self._ttl_hours)).isoformat()
        self._audit.append("activate", actor=actor, subject=session_id,
                           reason=result.message, vendor=session.vendor,
                           review_ref=session.review_ref)
        return session

    # ---------------------------------------------------------------- 撤销

    def revoke(self, session_id, actor="registry", reason=""):
        session = self._require(session_id)
        if session.state in (STATE_REVOKED, STATE_EXPIRED):
            return session                      # 幂等：终态重复撤销 = 无操作
        prev_state = session.state
        session.state = STATE_REVOKED
        self._audit.append("revoke", actor=actor, subject=session_id,
                           reason=reason or "revoked", vendor=session.vendor,
                           prev_state=prev_state)
        return session

    # ---------------------------------------------------------------- 到期

    def sweep_expired(self, now=None):
        """把 ACTIVE 且 expires_at 已过的会话置为 EXPIRED（返回本次置过期的列表）。"""
        now = now or self._clock()
        expired = []
        for session in self._sessions.values():
            if session.state == STATE_ACTIVE and session.expires_at and \
                    _to_dt(session.expires_at) < now:
                session.state = STATE_EXPIRED
                expired.append(session)
                self._audit.append("expire", actor="sweeper", subject=session.session_id,
                                   reason="session TTL reached (expires_at passed)",
                                   vendor=session.vendor)
        return expired

    # ---------------------------------------------------------------- 内部

    def _require(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session


def _to_dt(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

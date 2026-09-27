# coding: utf-8
"""凭据事件审计（append-only）——字段白名单 + token 永不落明文.

对齐 broker 调研的审计字段集（vendor 无关）：
``ts, cell_id, agent_session_id, principal, issuer, client_id, scope, event,
trigger, expires_at, mint_age_seconds, outcome, key_prefix, host, exit_code,
provider_latency_ms, detail``

红线：

- 日志永不输出完整 token / 任何密钥材料；凭据只允许以 :func:`key_prefix`
  指纹（sha256 前 12 位 + 长度）出现；
- 字段白名单之外的键直接丢弃（防把临时调试字段里的敏感值顺手写进审计）；
- 键名命中 ``TOKEN_KEYS``（token/access_token/…）直接 ``ValueError``——
  想记凭据请记 ``key_prefix(token)``；
- 底层复用 :class:`session_broker.audit.AuditLog`（append-only，无修改/删除接口）。
"""
from __future__ import annotations

import hashlib
import socket
import time

from ..audit import AuditLog

__all__ = ["AUDIT_FIELDS", "TOKEN_KEYS", "key_prefix", "CredentialAudit"]

AUDIT_FIELDS = (
    "ts", "cell_id", "agent_session_id", "principal", "issuer", "client_id",
    "scope", "event", "trigger", "expires_at", "mint_age_seconds", "outcome",
    "key_prefix", "host", "exit_code", "provider_latency_ms", "detail",
)

TOKEN_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "secret",
    "password", "credential", "api_key",
})


def key_prefix(token):
    """审计用凭据指纹：sha256 前 12 位 + 长度。永不输出明文。"""
    if not token:
        return "none"
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] \
        + ":len=%d" % len(token)


class CredentialAudit(object):
    """:class:`AuditLog` 的凭据事件门面（白名单 + 防泄漏守卫）。"""

    def __init__(self, audit=None, clock=None, sink=None):
        """
        ``sink``：JSONL 落盘目标（文件路径或可写对象），透传给底层 AuditLog。
        """
        if audit is None:
            audit = AuditLog(clock=clock, sink=sink)
        self._audit = audit
        self._host = socket.gethostname()

    def emit(self, event, cell_id="", **fields):
        """追加一条凭据事件；返回底层事件 dict（已脱敏）。"""
        if not event:
            raise ValueError("event is required")
        unknown = [k for k in fields if k not in AUDIT_FIELDS]
        leaked = [k for k in fields if str(k).lower() in TOKEN_KEYS]
        if leaked:
            raise ValueError(
                "refusing to audit raw credential keys %s; record "
                "key_prefix(token) instead" % sorted(leaked))
        payload = {k: fields[k] for k in AUDIT_FIELDS if k in fields}
        payload.setdefault("host", self._host)
        payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        return self._audit.append(
            type=str(event), actor="broker", subject=str(cell_id or ""),
            reason=str(payload.pop("detail", "") or ""),
            **payload)

    def events(self):
        return self._audit.events()

    def __len__(self):
        return len(self._audit)

# coding: utf-8
"""session-broker — C 路线（OAuth 中继）治理壳（WO-0008 / PROP-0001 12.1）.

MVP 边界：只做登记 / 红线 / 审计；**不做任何真实 OAuth 会话接入**——
激活前必须逐家 ToS 评审（红线：不得违反订阅条款使用 OAuth 会话）。
"""
from .audit import AuditLog
from .policy import (
    DEFAULT_SESSION_TTL_HOURS,
    REDLINE_TEXT,
    REVIEW_VALIDITY_DAYS,
    GateResult,
    PolicyDenied,
    ReviewRecord,
    ReviewStore,
    check_activation,
)
from .registry import (
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_REGISTERED,
    STATE_REVOKED,
    SessionRegistry,
    VendorSession,
    session_dict,
)

__version__ = "0.1.0"

__all__ = [
    "REDLINE_TEXT",
    "REVIEW_VALIDITY_DAYS",
    "DEFAULT_SESSION_TTL_HOURS",
    "AuditLog",
    "GateResult",
    "PolicyDenied",
    "ReviewRecord",
    "ReviewStore",
    "check_activation",
    "STATE_REGISTERED",
    "STATE_ACTIVE",
    "STATE_REVOKED",
    "STATE_EXPIRED",
    "VendorSession",
    "SessionRegistry",
    "session_dict",
    "__version__",
]

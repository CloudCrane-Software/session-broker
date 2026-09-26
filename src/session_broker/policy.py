# coding: utf-8
"""红线硬门（C 路线治理核心）.

**REDLINE_TEXT = "不得违反订阅条款使用 OAuth 会话"**

- ``activate()`` 无 review_ref，或 ToS 评审缺失 / 未批准 / 过期（90 天）→ 拒绝，
  会话状态停留在 REGISTERED；
- vendor 无 ToS 评审记录 → 永远无法激活；
- 拒绝必须落审计（deny 事件，reason 携带拒绝码与红线文本）。

本模块只做治理判定；它不持有、不接触、不传递任何登录态值。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

__all__ = [
    "REDLINE_TEXT",
    "REVIEW_VALIDITY_DAYS",
    "DEFAULT_SESSION_TTL_HOURS",
    "PolicyDenied",
    "GateResult",
    "ReviewRecord",
    "ReviewStore",
    "check_activation",
    "parse_review_date",
]

# 红线原文（进代码、进文档、进拒绝消息——三处一致）
REDLINE_TEXT = "不得违反订阅条款使用 OAuth 会话"

# ToS 评审有效期：评审日 + 90 天内可激活，过期必须重新评审
REVIEW_VALIDITY_DAYS = 90

# 会话默认 TTL（激活后 expires_at = now + TTL；到期由 sweep_expired 置 EXPIRED）
DEFAULT_SESSION_TTL_HOURS = 8


class PolicyDenied(Exception):
    """激活被红线门拒绝。``code`` 为拒绝码，``message`` 含红线文本。"""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__("[%s] %s" % (code, message))


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    code: str
    message: str


@dataclass(frozen=True)
class ReviewRecord:
    review_ref: str
    vendor: str
    reviewed_at: str          # ISO 日期 YYYY-MM-DD
    decision: str             # approved / rejected / ...
    tos_version: str


def parse_review_date(value):
    """ISO 日期字符串 → ``datetime.date``；非法抛 ValueError。"""
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("reviewed_at must be an ISO date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("reviewed_at must be an ISO date YYYY-MM-DD") from None


class ReviewStore:
    """ToS 评审记录册（内存；引用为主键，重复登记 = 复审更新，重置 90 天时钟）."""

    def __init__(self):
        self._records = {}

    def register(self, review_ref, vendor, reviewed_at, decision="approved",
                 tos_version=""):
        for name, val in (("review_ref", review_ref), ("vendor", vendor)):
            if not isinstance(val, str) or not val.strip():
                raise ValueError("%s must be a non-empty string" % name)
        parse_review_date(reviewed_at)
        rec = ReviewRecord(
            review_ref=review_ref,
            vendor=vendor,
            reviewed_at=reviewed_at,
            decision=str(decision),
            tos_version=str(tos_version or ""),
        )
        self._records[review_ref] = rec
        return rec

    def get(self, review_ref):
        return self._records.get(review_ref)

    def __contains__(self, review_ref):
        return review_ref in self._records

    def __len__(self):
        return len(self._records)


def check_activation(session, reviews, now):
    """红线门：返回 :class:`GateResult`。

    ``now`` 为 datetime；拒绝顺序：无引用 → 无记录 → vendor 不符 → 未批准 →
    过期（>90 天）。
    """
    if not session.review_ref:
        return GateResult(
            False, "REVIEW_REQUIRED",
            "%s；本会话未登记 ToS 评审引用（review_ref 缺失）" % REDLINE_TEXT,
        )
    record = reviews.get(session.review_ref)
    if record is None:
        return GateResult(
            False, "REVIEW_NOT_FOUND",
            "%s；vendor %r 无 ToS 评审记录（review_ref=%s）"
            % (REDLINE_TEXT, session.vendor, session.review_ref),
        )
    if record.vendor != session.vendor:
        return GateResult(
            False, "VENDOR_MISMATCH",
            "%s；评审记录属于 vendor %r，与会话 vendor %r 不符"
            % (REDLINE_TEXT, record.vendor, session.vendor),
        )
    if record.decision != "approved":
        return GateResult(
            False, "REVIEW_NOT_APPROVED",
            "%s；ToS 评审结论为 %r（非 approved）" % (REDLINE_TEXT, record.decision),
        )
    reviewed = parse_review_date(record.reviewed_at)
    now_date = now.date() if isinstance(now, datetime) else now
    age_days = (now_date - reviewed).days
    if age_days > REVIEW_VALIDITY_DAYS:
        return GateResult(
            False, "REVIEW_EXPIRED",
            "%s；ToS 评审已于 %d 天前过期（>%d 天，评审日 %s），必须重新评审"
            % (REDLINE_TEXT, age_days, REVIEW_VALIDITY_DAYS, record.reviewed_at),
        )
    return GateResult(
        True, "ALLOWED",
        "ToS 评审有效（评审日 %s，%d 天前）" % (record.reviewed_at, age_days),
    )

# coding: utf-8
"""append-only 审计事件流.

事件类型（本仓 MVP）：register / activate / revoke / deny / expire。
每事件含：seq、ts、type、actor、subject、reason（+ 允许的附加字段）。

红线：事件里只允许出现**引用与字段名**（session_id、review_ref、拒绝码），
任何密钥值、登录态值、subject 明文一律不得进入事件。
存储为内存列表 + 可选 JSONL 追加文件；不提供修改与删除接口（append-only）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

__all__ = ["AuditLog"]


def _default_clock():
    return datetime.now(timezone.utc)


def _iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class AuditLog:
    """append-only 审计日志（内存 + 可选 JSONL sink）."""

    def __init__(self, clock=None, sink=None):
        """``sink``：可写文本对象（以 "a" 语义逐行追加 JSONL）或文件路径。"""
        self._clock = clock or _default_clock
        self._events = []
        self._seq = 0
        self._sink_path = None
        self._sink_fh = None
        if sink is None:
            pass
        elif hasattr(sink, "write"):
            self._sink_fh = sink
        else:
            self._sink_path = str(sink)

    def append(self, type, actor, subject, reason="", **extra):
        """追加一条事件。extra 仅允许标量引用字段（str/int/float/bool/None）。"""
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": _iso(self._clock()),
            "type": str(type),
            "actor": str(actor),
            "subject": str(subject),
            "reason": str(reason or ""),
        }
        for k, v in extra.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                event[str(k)] = v
        self._events.append(event)
        line = json.dumps(event, ensure_ascii=False)
        if self._sink_fh is not None:
            self._sink_fh.write(line + "\n")
            self._sink_fh.flush()
        elif self._sink_path is not None:
            with open(self._sink_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return dict(event)

    def events(self):
        """只读视图（元组拷贝，防外部改动）。"""
        return tuple(self._events)

    def by_type(self, type):
        return tuple(e for e in self._events if e["type"] == type)

    def __len__(self):
        return len(self._events)

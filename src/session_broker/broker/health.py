# coding: utf-8
"""健康端点数据源——cell / 调度器 / 审计的只读快照.

快照只含**引用、计数与时间**，任何凭据值 / token / 密钥材料零出现；
供 REST API 挂 ``GET /broker/health``（见 :mod:`session_broker.api`）。
"""
from __future__ import annotations

__all__ = ["BrokerHealth"]


class BrokerHealth(object):
    """聚合 cell 注册表、调度器摘要与审计长度的只读健康快照。"""

    def __init__(self, audit=None, clock=None):
        self._cells = []            # (cell_id, vendor, leader_socket, config_path|None)
        self._schedulers = {}       # name -> summary dict
        self._audit = audit         # 只取 len()，不暴露事件内容给健康端点
        self._clock = clock

    # ------------------------------------------------------------- 注册

    def register_cell(self, cell):
        entry = {"cell_id": cell.cell_id, "vendor": cell.vendor,
                 "leader_socket": cell.leader_socket,
                 "config_path": cell.config_path if cell.config_writer else None}
        self._cells = [e for e in self._cells if e["cell_id"] != entry["cell_id"]]
        self._cells.append(entry)
        return entry

    def register_scheduler(self, name, scheduler, expires_at_epoch=None):
        """登记一个刷新调度器的摘要（ TTL/提前量/下一次应刷新时刻）。"""
        summary = {
            "ttl_seconds": scheduler.ttl_seconds,
            "lead_seconds": scheduler.lead_seconds,
            "lead_lt_ttl": scheduler.lead_seconds < scheduler.ttl_seconds,
            "backoff_attempts": scheduler.backoff_attempts,
        }
        if expires_at_epoch is not None:
            summary["refresh_due_at"] = scheduler.refresh_due_at(expires_at_epoch)
            summary["seconds_until_refresh"] = scheduler.seconds_until_refresh(
                expires_at_epoch)
        self._schedulers[str(name)] = summary
        return summary

    # ------------------------------------------------------------- 快照

    def snapshot(self):
        snap = {
            "status": "ok",
            "cells": sorted(self._cells, key=lambda e: e["cell_id"]),
            "cell_count": len(self._cells),
            "schedulers": dict(sorted(self._schedulers.items())),
            "audit_events": len(self._audit) if self._audit is not None else 0,
        }
        if self._clock is not None:
            snap["time"] = self._clock.now()
        return snap

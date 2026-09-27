# coding: utf-8
"""刷新调度器 RefreshScheduler——提前量 < TTL 硬约束 + 401 防环路 + 有界退避.

语义（对齐各 vendor CLI 的 external refresher 行为，vendor 无关）：

- **主动刷新窗口**：``now + lead_seconds >= expires_at`` 即到期。``lead_seconds``
  （默认 300s）**必须严格小于 TTL**——构造时校验，违反直接 ``ValueError``。
  （实测教训：vendor CLI 的"提前失效秒数"若 >= token TTL，每次铸出的 token 一出生
  就处于刷新窗口，pre-send 刷新进入死循环。）
- **401 防环路**：新铸 token 在 ``fresh_reject_seconds``（默认 30s）内即被 401 拒绝
  → 判定为环境性失败，**不再重取**，应升级为交互式登录（需要用户在场）。
- **退避**：1,2,4,8,15,30,60s 封顶（有界指数退避）；成功铸币后 ``reset_backoff()``。
- 全部判断经注入的 :class:`~session_broker.broker.clock.Clock`，可用 MockClock 压缩验证。
"""
from __future__ import annotations

import random

from .clock import SystemClock

__all__ = ["DEFAULT_LEAD_SECONDS", "DEFAULT_FRESH_REJECT_SECONDS",
           "DEFAULT_BACKOFF", "RefreshScheduler"]

DEFAULT_LEAD_SECONDS = 300
DEFAULT_FRESH_REJECT_SECONDS = 30
DEFAULT_BACKOFF = (1, 2, 4, 8, 15, 30, 60)


class RefreshScheduler(object):
    """无状态判断 + 有界重试记忆。线程不安全（单 broker 事件循环内使用）。"""

    def __init__(self, ttl_seconds, lead_seconds=DEFAULT_LEAD_SECONDS,
                 fresh_reject_seconds=DEFAULT_FRESH_REJECT_SECONDS,
                 backoff=DEFAULT_BACKOFF, clock=None, jitter=False, rng=None):
        ttl_seconds = int(ttl_seconds)
        lead_seconds = int(lead_seconds)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive, got %r" % ttl_seconds)
        # 硬约束：刷新提前量必须 < TTL，否则 token 一出生就在刷新窗口 → 死循环
        if lead_seconds <= 0:
            raise ValueError("lead_seconds must be positive, got %r" % lead_seconds)
        if lead_seconds >= ttl_seconds:
            raise ValueError(
                "lead_seconds (%d) must be strictly less than ttl_seconds (%d); "
                "otherwise every minted token is born inside its own refresh window"
                % (lead_seconds, ttl_seconds))
        self.ttl_seconds = ttl_seconds
        self.lead_seconds = lead_seconds
        self.fresh_reject_seconds = int(fresh_reject_seconds)
        self.backoff = tuple(float(x) for x in backoff) or (1.0,)
        self.clock = clock or SystemClock()
        self.jitter = bool(jitter)
        self._rng = rng or random.Random(0)
        self._attempt = 0

    # ------------------------------------------------------------- 窗口判断

    def should_refresh(self, expires_at_epoch, now=None):
        """是否进入主动刷新窗口（``now + lead >= expires_at``）。"""
        now = self.clock.now() if now is None else now
        return now + self.lead_seconds >= float(expires_at_epoch)

    def refresh_due_at(self, expires_at_epoch):
        """按计划，下一次刷新应发生的时刻（expires_at - lead）。"""
        return float(expires_at_epoch) - self.lead_seconds

    def seconds_until_refresh(self, expires_at_epoch, now=None):
        now = self.clock.now() if now is None else now
        return self.refresh_due_at(expires_at_epoch) - now

    # ------------------------------------------------------------- 401 环路

    def on_rejected(self, fetched_at_epoch, now=None):
        """401 后是否允许再取一次。

        返回 True=可重取（token 已不够新）；False=新铸即拒，停止重取、升级人工登录。
        """
        now = self.clock.now() if now is None else now
        return now - float(fetched_at_epoch) >= self.fresh_reject_seconds

    # ------------------------------------------------------------- 退避

    def backoff_delay(self):
        """本次失败后应等待的秒数（有界指数 + 可选 jitter），并记一次失败。"""
        idx = min(self._attempt, len(self.backoff) - 1)
        delay = self.backoff[idx]
        if self.jitter:
            delay = delay * (0.5 + self._rng.random() * 0.5)
        self._attempt += 1
        return delay

    def reset_backoff(self):
        """铸币/刷新成功后调用：退避归零。"""
        self._attempt = 0

    @property
    def backoff_attempts(self):
        return self._attempt

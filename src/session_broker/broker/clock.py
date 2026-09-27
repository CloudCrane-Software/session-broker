# coding: utf-8
"""时钟抽象——真实时钟与可推进的 MockClock（供压缩时间仿真用）.

 broker 的刷新调度（:mod:`session_broker.broker.scheduler`）全部经 ``Clock.now()``
 取当前 epoch 秒；测试用 ``MockClock`` 手动 ``advance()`` 推进，即可把"7 天 TTL、
 数月会话周期"压缩到毫秒级验证，无需真实登录、无需真实等待。
"""
from __future__ import annotations

import time

__all__ = ["Clock", "SystemClock", "MockClock"]


class Clock(object):
    """时钟接口：``now()`` 返回 epoch 秒（float）。"""

    def now(self):
        raise NotImplementedError


class SystemClock(Clock):
    """真实系统时钟（time.time）。"""

    def now(self):
        return time.time()


class MockClock(Clock):
    """可手动推进的仿真时钟（epoch 秒）.

    用法::

        clk = MockClock(start=1_000_000.0)
        clk.advance(3600)      # 时间前进 1 小时
        assert clk.now() == 1_003_600.0
    """

    def __init__(self, start=0.0):
        self._now = float(start)

    def now(self):
        return self._now

    def advance(self, seconds):
        """向前推进（不允许倒流——倒流会破坏退避/ freshness 语义）。"""
        seconds = float(seconds)
        if seconds < 0:
            raise ValueError("MockClock cannot go backwards (got %r)" % seconds)
        self._now += seconds
        return self._now

    def set(self, epoch_seconds):
        """直接跳到某时刻（仅测试构造用；同样不允许倒流）。"""
        epoch_seconds = float(epoch_seconds)
        if epoch_seconds < self._now:
            raise ValueError("MockClock cannot go backwards (%r < %r)"
                             % (epoch_seconds, self._now))
        self._now = epoch_seconds
        return self._now

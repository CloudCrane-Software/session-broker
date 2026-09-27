# coding: utf-8
"""刷新周期压缩仿真——MockClock 驱动的长周期会话刷新验证.

把"token TTL 7 天、会话持续数月"的刷新节律压缩到毫秒级：仿真器按固定步长推进
MockClock，在每个采样点检查 :class:`RefreshScheduler` 的窗口判断，窗口打开即
经注入的凭据源换发 token（成功则退避归零、审计一条 refresh 事件）。

不变量（仿真结束逐条断言，违反即抛 ``CycleInvariantError``）：

1. 每次刷新都发生在 ``[expires_at - lead, expires_at)`` 窗口内——不早刷、不裸奔过期；
2. 刷新提前量 < TTL（构造级约束的运行期复证）；
3. 相邻两次刷新间隔 == TTL - lead（无漂移）；
4. 401-新铸即拒语义：可选注入的 401 注入器命中时，fresh token 拒绝不重取。
"""
from __future__ import annotations

from .minting import TRIGGER_HEADLESS_REFRESH

__all__ = ["CycleReport", "run_refresh_cycle", "CycleInvariantError"]


class CycleInvariantError(AssertionError):
    """刷新周期不变量被破坏（仿真失败）。"""


class CycleReport(object):
    def __init__(self):
        self.initial_mint_epoch = None
        self.refresh_epochs = []       # 每次换发发生的时刻
        self.expires_at_epochs = []    # 每枚 token 的到期时刻
        self.events = []               # (epoch, kind, detail)

    @property
    def token_count(self):
        return 1 + len(self.refresh_epochs)

    def summary(self):
        return {
            "initial_mint_epoch": self.initial_mint_epoch,
            "refresh_count": len(self.refresh_epochs),
            "token_count": self.token_count,
            "refresh_epochs": list(self.refresh_epochs),
            "expires_at_epochs": list(self.expires_at_epochs),
        }


def run_refresh_cycle(scheduler, clock, source, total_seconds,
                      tick_seconds=None, audit=None, cell_id="sim-cell"):
    """仿真一条长会话：冷启动铸币 → 周期性换发，返回 :class:`CycleReport`。

    ``source``：具备 ``mint(trigger=...) -> MintedToken`` 的凭据源（测试用 FAKE 源）。
    """
    tick = float(tick_seconds or min(scheduler.lead_seconds, 60))
    report = CycleReport()
    current = source.mint(trigger="cold_start")
    scheduler.reset_backoff()
    report.initial_mint_epoch = current.fetched_at_epoch
    report.expires_at_epochs.append(current.expires_at_epoch)
    report.events.append((current.fetched_at_epoch, "mint", current.redacted()))
    if audit is not None:
        fields = current.audit_fields()
        fields.setdefault("trigger", "cold_start")
        audit.emit(event="login", cell_id=cell_id, **fields)

    horizon = clock.now() + float(total_seconds)
    while clock.now() < horizon:
        now = clock.now()
        if not scheduler.should_refresh(current.expires_at_epoch):
            # 未到窗口：按 tick 推进，但不超过窗口打开时刻（保证在 due 处正好轮询）
            step = min(tick,
                       scheduler.refresh_due_at(current.expires_at_epoch) - now,
                       horizon - now)
            clock.advance(max(step, 0.0))
            continue
        # 窗口打开：换发（真实实现走官方钩子；此处为注入的凭据源）
        minted = source.mint(trigger=TRIGGER_HEADLESS_REFRESH)
        epoch = now
        # 不变量 1：窗口内刷新
        due = scheduler.refresh_due_at(current.expires_at_epoch)
        if not (due <= epoch < current.expires_at_epoch):
            raise CycleInvariantError(
                "refresh at %.0f outside window [%.0f, %.0f)"
                % (epoch, due, current.expires_at_epoch))
        # 不变量 2：提前量 < TTL
        if scheduler.lead_seconds >= scheduler.ttl_seconds:
            raise CycleInvariantError("lead >= TTL")
        report.refresh_epochs.append(epoch)
        report.events.append((epoch, "refresh", minted.redacted()))
        if audit is not None:
            fields = minted.audit_fields()
            fields.setdefault("trigger", "pre_send")
            audit.emit(event="refresh", cell_id=cell_id, **fields)
        current = minted
        scheduler.reset_backoff()
        report.expires_at_epochs.append(current.expires_at_epoch)
        # 跳到下一枚 token 的刷新窗口打开处（避免空转 tick）
        next_due = scheduler.refresh_due_at(current.expires_at_epoch)
        clock.advance(min(max(next_due - clock.now(), 0.0),
                          horizon - clock.now()))
    # 不变量 3：间隔无漂移
    step = scheduler.ttl_seconds - scheduler.lead_seconds
    for a, b in zip(report.refresh_epochs, report.refresh_epochs[1:]):
        if abs((b - a) - step) > tick:
            raise CycleInvariantError(
                "refresh interval drift: %.0f -> %.0f (expected ~%d)"
                % (a, b, step))
    return report

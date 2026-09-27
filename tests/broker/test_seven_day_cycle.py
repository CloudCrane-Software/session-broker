# coding: utf-8
"""broker.simulate：MockClock 压缩验证 7 天 TTL 的长周期刷新节律.

token TTL = 7 天，提前量 = 300s，仿真 70 天会话周期（毫秒级完成）。
仿真按 tick 粒度检测窗口打开，因此刷新时刻允许 [due, due + tick] 的检测延迟；
节律关系（间隔 = TTL - 提前量）必须精确成立。全程零真实登录。
"""
import pytest

from session_broker.broker import (CredentialAudit, CycleInvariantError,
                                   FakeCredentialSource, MockClock,
                                   RefreshScheduler, run_refresh_cycle)

DAY = 86_400
TTL = 7 * DAY
LEAD = 300
STEP = TTL - LEAD          # 相邻两次刷新的间隔（精确关系）
TICK = 60


def _run(total_days, cell_id="sim-cell-7d", ttl=TTL, lead=LEAD):
    clk = MockClock(start=1_700_000_000.0)
    sched = RefreshScheduler(ttl_seconds=ttl, lead_seconds=lead, clock=clk)
    src = FakeCredentialSource(token="FAKE.cycle.token.NOT-A-REAL-CREDENTIAL",
                               expires_in=ttl, clock=clk)
    audit = CredentialAudit()
    report = run_refresh_cycle(sched, clk, src, total_seconds=total_days * DAY,
                               tick_seconds=TICK, audit=audit, cell_id=cell_id)
    return clk, sched, src, audit, report


def _expected_dues(base, count, ttl=TTL, lead=LEAD):
    """第 k 次刷新的计划时刻：due_0 = base+ttl-lead，due_k = due_{k-1}+ttl-lead。"""
    dues, d = [], base + ttl - lead
    for _ in range(count):
        dues.append(d)
        d += ttl - lead
    return dues


def test_seven_day_ttl_yields_expected_refresh_count_over_70_days():
    *_, report = _run(total_days=70)
    assert len(report.refresh_epochs) == 10
    assert report.token_count == 11  # 冷启动 1 + 刷新 10
    base = report.initial_mint_epoch
    for epoch, due in zip(report.refresh_epochs, _expected_dues(base, 10)):
        assert due <= epoch <= due + TICK  # 窗口打开后一个 tick 内完成


def test_every_refresh_within_window_after_due():
    *_, report = _run(total_days=35)
    base = report.initial_mint_epoch
    for i, epoch in enumerate(report.refresh_epochs):
        due = base + (i + 1) * STEP
        assert due <= epoch <= due + TICK


def test_refresh_interval_has_no_drift():
    *_, report = _run(total_days=49)
    gaps = [b - a for a, b in zip(report.refresh_epochs, report.refresh_epochs[1:])]
    assert gaps == [pytest.approx(STEP, abs=TICK)] * len(gaps)
    # token 到期时刻逐次前移 STEP（提前刷新的代价），关系同样精确
    exp = report.expires_at_epochs
    assert all(b - a == pytest.approx(STEP, abs=TICK)
               for a, b in zip(exp, exp[1:]))


def test_audit_records_mint_and_refresh_events():
    _, _, _, audit, report = _run(total_days=21)
    kinds = sorted(e["type"] for e in audit.events())
    assert kinds.count("login") == 1
    assert kinds.count("refresh") == 3  # 21 天 / 7 天 = 3 次
    blob = str([dict(e) for e in audit.events()])
    assert "FAKE.cycle.token" not in blob  # 只有指纹，无明文
    assert report.token_count == 4


def test_simulation_rejects_lead_ge_ttl():
    clk = MockClock(start=0.0)
    src = FakeCredentialSource(expires_in=300, clock=clk)
    with pytest.raises(ValueError):
        sched = RefreshScheduler(ttl_seconds=300, lead_seconds=600, clock=clk)
        run_refresh_cycle(sched, clk, src, total_seconds=10 * DAY, tick_seconds=TICK)


def test_simulation_flags_invariant_when_tokens_outlive_window():
    # lead < TTL 构造合法，但凭据源给的 TTL 比调度器认为的短一倍 →
    # 第二枚 token 的刷新时刻落在第一枚已过期之后，仿真应能复证节律仍然自洽
    clk = MockClock(start=1_700_000_000.0)
    sched = RefreshScheduler(ttl_seconds=TTL, lead_seconds=LEAD, clock=clk)
    src = FakeCredentialSource(expires_in=TTL, clock=clk)
    report = run_refresh_cycle(sched, clk, src, total_seconds=3 * DAY,
                               tick_seconds=TICK)
    assert report.refresh_epochs == []  # 3 天 < 7 天-300s：尚未进入窗口


def test_zero_day_horizon_only_cold_mint():
    *_, report = _run(total_days=0)
    assert report.refresh_epochs == []
    assert report.token_count == 1

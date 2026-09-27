# coding: utf-8
"""broker.scheduler：提前量 < TTL 硬约束、窗口判断、401 防环路、退避。"""
import pytest

from session_broker.broker import MockClock, RefreshScheduler

DAY = 86_400


def _sched(**kw):
    base = dict(ttl_seconds=7 * DAY, lead_seconds=300,
                fresh_reject_seconds=30, clock=MockClock(start=1_000_000.0))
    base.update(kw)
    return RefreshScheduler(**base)


def test_lead_must_be_strictly_less_than_ttl():
    with pytest.raises(ValueError):
        RefreshScheduler(ttl_seconds=3600, lead_seconds=3600)
    with pytest.raises(ValueError):
        RefreshScheduler(ttl_seconds=3600, lead_seconds=7200)  # PoC 踩坑值


def test_should_refresh_window_boundaries():
    s = _sched()
    exp = 1_000_000.0 + 7 * DAY
    # 提前 301s：窗口未开
    assert not s.should_refresh(exp, now=exp - 301)
    # 提前 300s：窗口正开（now + lead >= exp）
    assert s.should_refresh(exp, now=exp - 300)
    # 已过期：必刷
    assert s.should_refresh(exp, now=exp + 1)


def test_refresh_due_at_is_expiry_minus_lead():
    s = _sched()
    exp = 5_000_000.0
    assert s.refresh_due_at(exp) == exp - 300
    assert s.seconds_until_refresh(exp, now=exp - 600) == 300


def test_fresh_token_rejection_guard():
    s = _sched()
    now = 1_000_000.0
    # 新铸 29s 即被 401 → 不许再取（防错误循环）
    assert not s.on_rejected(now, now=now + 29)
    # 31s 后仍 401 → 可重取一次
    assert s.on_rejected(now, now=now + 31)


def test_backoff_is_bounded_and_resettable():
    clk = MockClock(start=0.0)
    s = _sched(clock=clk, backoff=(1, 2, 4))
    assert [s.backoff_delay() for _ in range(5)] == [1, 2, 4, 4, 4]  # 封顶
    s.reset_backoff()
    assert s.backoff_delay() == 1
    assert s.backoff_attempts == 1


def test_backoff_jitter_stays_within_half_to_one():
    s = _sched(backoff=(10,), jitter=True)
    for _ in range(20):
        d = s.backoff_delay()
        assert 5.0 <= d <= 10.0
        s.reset_backoff()


def test_invalid_ttl_and_lead_values():
    with pytest.raises(ValueError):
        RefreshScheduler(ttl_seconds=0)
    with pytest.raises(ValueError):
        RefreshScheduler(ttl_seconds=3600, lead_seconds=-1)

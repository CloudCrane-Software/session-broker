# coding: utf-8
"""broker.clock：MockClock 推进 / 不可倒流。"""
import pytest

from session_broker.broker import MockClock, SystemClock


def test_mock_clock_advances_by_steps():
    clk = MockClock(start=1_000.0)
    assert clk.now() == 1_000.0
    clk.advance(60)
    clk.advance(0.5)
    assert clk.now() == pytest.approx(1_060.5)


def test_mock_clock_cannot_go_backwards():
    clk = MockClock(start=100.0)
    with pytest.raises(ValueError):
        clk.advance(-1)
    with pytest.raises(ValueError):
        clk.set(99.0)
    clk.set(200.0)  # 前跳允许
    assert clk.now() == 200.0


def test_system_clock_returns_epoch_float():
    assert isinstance(SystemClock().now(), float)

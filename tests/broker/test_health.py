# coding: utf-8
"""broker.health + /broker/health 路由。"""
from session_broker.api import BrokerApp
from session_broker.broker import (BrokerHealth, CredentialCell, MockClock,
                                   RefreshScheduler)

DAY = 86_400


def _health():
    clk = MockClock(start=1_000.0)
    h = BrokerHealth(clock=clk)
    cell = CredentialCell("irrelevant/root", "cell-7", vendor="demo",
                          config_writer=lambda c: "k = 1\n")
    h.register_cell(cell)
    sched = RefreshScheduler(ttl_seconds=7 * DAY, lead_seconds=300, clock=clk)
    h.register_scheduler("cell-7", sched, expires_at_epoch=1_000.0 + 7 * DAY)
    return h, clk


def test_snapshot_contains_cells_schedulers_audit_counts_only():
    h, clk = _health()
    snap = h.snapshot()
    assert snap["status"] == "ok"
    assert snap["cell_count"] == 1
    assert snap["cells"][0]["cell_id"] == "cell-7"
    assert snap["cells"][0]["leader_socket"].endswith("leader-cell-7.sock")
    s = snap["schedulers"]["cell-7"]
    assert s["lead_lt_ttl"] is True
    assert s["ttl_seconds"] == 7 * DAY
    assert s["refresh_due_at"] == 1_000.0 + 7 * DAY - 300
    assert snap["time"] == 1_000.0
    # 快照里没有凭据类字段
    blob = str(snap)
    for bad in ("access_token", "token", "secret"):
        assert bad not in blob


def test_api_broker_health_route_via_dispatch():
    h, _ = _health()
    app = BrokerApp(broker_health=h)
    status, payload = app.dispatch("GET", "/broker/health")
    assert status == 200
    assert payload["cell_count"] == 1


def test_api_broker_health_not_configured_returns_501():
    app = BrokerApp()
    status, payload = app.dispatch("GET", "/broker/health")
    assert status == 501
    assert payload["error"]["code"] == "BROKER_HEALTH_NOT_CONFIGURED"


def test_api_broker_health_rejects_post():
    h, _ = _health()
    app = BrokerApp(broker_health=h)
    status, payload = app.dispatch("POST", "/broker/health", body={})
    assert status == 405

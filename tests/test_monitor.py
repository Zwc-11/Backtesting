import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
from conftest import bars_at
from fastapi.testclient import TestClient

from xasset.config import Instrument
from xasset.features.returns import trailing_features
from xasset.monitor.alerts import deliver, enqueue, recent
from xasset.monitor.api import app
from xasset.monitor.config import MonitorConfig, load
from xasset.monitor.detectors import alert, detect
from xasset.monitor.feeds import market_window, public_bars
from xasset.monitor.service import cycle
from xasset.store.writer import write_json


def kraken():
    return Instrument(
        id="BTC_KRAKEN",
        asset_class="crypto",
        tier="A",
        venue="KRAKEN",
        session="all",
        kraken_symbol="XBTUSD",
        sources=["kraken"],
    )


def test_public_feed_rejects_unfinished_candle_and_wrong_symbol():
    start = datetime(2026, 10, 8, 12, tzinfo=UTC)
    rows = [
        [int((start + timedelta(minutes=i)).timestamp()), 100, 102, 99, 101, 100, 2, 1]
        for i in range(3)
    ]
    bars = public_bars(
        {"error": [], "result": {"XXBTZUSD": rows, "last": 0}},
        kraken(),
        start,
        start + timedelta(minutes=2),
    )
    assert bars.height == 2
    hyper = load(Path("config/monitor.yaml")).instruments[-1]
    with pytest.raises(ValueError, match="mismatch"):
        public_bars([{"s": "ETH", "i": "1m"}], hyper, start, start + timedelta(minutes=2))


def test_closed_equity_session_is_not_a_stale_weekend_feed():
    item = load(Path("config/monitor.yaml")).instruments[0]
    start, end, is_open = market_window(item, datetime(2026, 10, 10, 12, tzinfo=UTC), 180)
    assert not is_open
    assert end == datetime(2026, 10, 9, 20, tzinfo=UTC)
    assert end - start == timedelta(minutes=180)


def test_monitor_uses_shared_features_and_is_invariant_to_future_data():
    start = datetime(2026, 10, 8, 12, tzinfo=UTC)
    stamps = [start + timedelta(minutes=i + 1) for i in range(180)]
    bars = bars_at(stamps, "BTC_KRAKEN")
    config = MonitorConfig(instruments=[kraken()], volatility_window=20)
    cutoff = stamps[89]
    health = [{"symbol": "BTC_KRAKEN", "status": "healthy"}]
    full, latest = detect(bars, config, cutoff, health)
    truncated, shorter = detect(bars.filter(pl.col("ts_end") <= cutoff), config, cutoff, health)
    assert full == truncated and latest == shorter
    shared = trailing_features(bars.head(90), 20)
    assert latest[0]["return_1m"] == shared["return_1m"][-1]
    assert latest[0]["volatility"] == shared["volatility"][-2]


def test_deduplication_and_cooldown_survive_restarts(tmp_path):
    at = datetime(2026, 10, 8, 12, tzinfo=UTC)
    first = alert("move", "BTC", at, "medium", "Observed move")
    assert enqueue(tmp_path, [first], 1800) == 1
    assert enqueue(tmp_path, [first], 1800) == 0
    second = alert("move", "BTC", at + timedelta(minutes=1), "medium", "Observed move")
    assert enqueue(tmp_path, [second], 1800) == 0
    third = alert("move", "BTC", at + timedelta(minutes=31), "medium", "Observed move")
    assert enqueue(tmp_path, [third], 1800) == 1
    assert [event["status"] for event in recent(tmp_path)] == ["queued", "suppressed", "queued"]


def test_outbound_messages_stay_disabled_even_with_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config = MonitorConfig(instruments=[kraken()], alert_mode="telegram")

    def forbidden(request):
        pytest.fail("Disabled delivery sent a message")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        assert deliver(tmp_path, config, client) == {"status": "log_only", "sent": 0}


def test_uncertain_delivery_is_not_retried_and_does_not_expose_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config = MonitorConfig(instruments=[kraken()], alert_mode="telegram", delivery_enabled=True)
    enqueue(tmp_path, [alert("move", "BTC", datetime.now(UTC), "medium", "Synthetic move")], 60)
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("No receipt", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        deliver(tmp_path, config, client)
        deliver(tmp_path, config, client)
    assert len(requests) == 1
    assert recent(tmp_path)[0]["status"] == "delivery_uncertain"
    assert "synthetic-secret" not in json.dumps(recent(tmp_path))


def test_failed_feed_is_reported_without_erasing_successful_capture(tmp_path):
    config = MonitorConfig(
        instruments=[kraken(), load(Path("config/monitor.yaml")).instruments[-1]]
    )
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)

    def reply(request):
        if request.url.host == "api.hyperliquid.xyz":
            return httpx.Response(403)
        stamps = [now - timedelta(minutes=i) for i in range(181, 0, -1)]
        rows = [[int(t.timestamp()), 100, 102, 99, 101, 100, 2, 1] for t in stamps]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": rows, "last": 0}})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = cycle(tmp_path, config, client, now)
    assert [h["status"] for h in result["health"]] == ["healthy", "failed"]
    assert result["orders_sent"] == 0 and result["delivery"]["sent"] == 0
    assert (tmp_path / "live/bars/crypto/BTC_KRAKEN/2026-10.parquet").exists()
    assert any(e["kind"] == "data_health" for e in result["alerts"])


def test_private_dashboard_authentication_and_path_restrictions(tmp_path, monkeypatch):
    monkeypatch.setenv("XASSET_DASHBOARD_TOKEN", "synthetic-private-token")
    write_json(tmp_path / "monitor/latest.json", {"health": [], "orders_sent": 0})
    with TestClient(app(tmp_path)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/summary").status_code == 401
        assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
        assert (
            client.post("/api/session", json={"token": "synthetic-private-token"}).status_code
            == 200
        )
        response = client.get("/api/summary")
        assert response.status_code == 200
        assert response.json()["orders_enabled"] is False
        assert "synthetic-private-token" not in response.text
        assert client.get("/assets/registry.duckdb").status_code == 404
        assert client.get("/data/registry.duckdb").status_code == 404
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["orders_sent"] == 0


def test_recent_price_does_not_hide_missing_minutes(tmp_path):
    config = MonitorConfig(instruments=[kraken()])
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)

    def reply(request):
        stamps = [now - timedelta(minutes=i) for i in range(181, 0, -1) if i != 45]
        rows = [[int(t.timestamp()), 100, 102, 99, 101, 100, 2, 1] for t in stamps]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": rows, "last": 0}})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = cycle(tmp_path, config, client, now)
    assert result["health"][0]["status"] == "missing"
    assert result["health"][0]["missing_minutes"] == 1
    assert result["healthy_feeds"] == 0
    assert any(e["kind"] == "data_health" for e in result["alerts"])

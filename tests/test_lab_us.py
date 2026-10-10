"""The large-cap US universe and Alpaca minute ingest, against a fake Alpaca server."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest

from xasset.lab import us
from xasset.lab.bars import read_lab_bars
from xasset.lab.universe import Universe


def fake_alpaca(request: httpx.Request) -> httpx.Response:
    assert request.headers["APCA-API-KEY-ID"] == "key"
    assert request.url.params["feed"] == "sip" and request.url.params["adjustment"] == "raw"
    symbol = request.url.path.split("/")[3]
    start = datetime.fromisoformat(request.url.params["start"])
    end = datetime.fromisoformat(request.url.params["end"])
    rank = list(us.CANDIDATES).index(symbol) if symbol in us.CANDIDATES else 0
    bars = []
    if request.url.params["timeframe"] == "1Day":
        day = start
        while day < end:
            if day.weekday() < 5:
                bars.append({"t": day.strftime("%Y-%m-%dT05:00:00Z"), "o": 10, "h": 11, "l": 9,
                             "c": 10, "v": 1000.0 / (rank + 1), "vw": 10.0, "n": 5})  # fmt: skip
            day += timedelta(days=1)
        return httpx.Response(200, json={"bars": bars, "symbol": symbol})
    # Minute bars: one pre-market bar, two regular-session bars, paginated.
    token = request.url.params.get("page_token")
    day = start.replace(day=2) if start.day == 1 else start
    while day.weekday() >= 5:
        day += timedelta(days=1)
    if token is None:
        bars = [
            {"t": day.strftime("%Y-%m-%dT12:00:00Z"), "o": 1, "h": 1, "l": 1, "c": 1,
             "v": 1, "vw": 1},
            {"t": day.strftime("%Y-%m-%dT13:30:00Z"), "o": 10, "h": 10.5, "l": 9.5, "c": 10.2,
             "v": 200, "vw": 10.1, "n": 7},
        ]  # fmt: skip
        return httpx.Response(200, json={"bars": bars, "next_page_token": "p2"})
    bars = [{"t": day.strftime("%Y-%m-%dT13:31:00Z"), "o": 10.2, "h": 10.4, "l": 10.0, "c": 10.3,
             "v": 100, "vw": 10.25, "n": 3}]  # fmt: skip
    return httpx.Response(200, json=json.loads(json.dumps({"bars": bars, "next_page_token": None})))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> httpx.Client:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    return httpx.Client(transport=httpx.MockTransport(fake_alpaca))


def test_monthly_members_rank_by_dollar_volume_and_keep_the_etfs(
    tmp_path: Path, client: httpx.Client
) -> None:
    for symbol in [*us.ETFS, *list(us.CANDIDATES)[:30]]:
        us.fetch_daily(client, tmp_path, symbol, date(2025, 12, 1), date(2026, 3, 1))
    members = us.monthly_members(tmp_path, date(2026, 1, 1), date(2026, 2, 1), top=10)
    assert set(members) == {"2026-01", "2026-02"}
    first = members["2026-01"]
    assert first[: len(us.ETFS)] == list(us.ETFS)
    assert first[len(us.ETFS) :] == list(us.CANDIDATES)[:10]  # rank order by dollar volume
    document = us.universe_document(members, 10)
    universe = Universe.model_validate(document)
    assert universe.benchmark == "SPY"
    assert universe.get("AAPL").sector == "XLK" and universe.get("SPY").breadth_member is False
    assert {p.leader for p in universe.pairs} <= set(us.ETFS)


def test_minute_ingest_keeps_the_regular_session_with_trade_vwap_notional(
    tmp_path: Path, client: httpx.Client
) -> None:
    # June: New York is on daylight time, so the session opens at 13:30 UTC.
    report = us.ingest_minutes(client, tmp_path, "AAPL", {"2026-06"})
    assert report["months"] == [{"month": "2026-06", "rows": 2}]
    frame = read_lab_bars(tmp_path, us.SOURCE, "AAPL", datetime(2026, 6, 1, tzinfo=UTC),
                          datetime(2026, 7, 1, tzinfo=UTC))  # fmt: skip
    assert frame.height == 2  # the 12:00 UTC pre-market bar is dropped
    assert frame["notional"].to_list() == pytest.approx([10.1 * 200, 10.25 * 100])
    assert frame["buy_notional"].is_null().all()  # no aggressor labels from Alpaca bars
    assert frame["ts_end"][0] == datetime(
        2026, 6, 2, 13, 31, tzinfo=UTC
    )  # first weekday the fake serves
    # In March (before daylight time) the same 13:30 UTC bars are pre-market: an empty month.
    early = us.ingest_minutes(client, tmp_path, "MSFT", {"2026-03"})
    assert early["months"] == [{"month": "2026-03", "rows": 0}]
    empty = pl.read_parquet(tmp_path / "lab" / "bars" / us.SOURCE / "MSFT" / "2026-03.parquet")
    assert empty.height == 0 and empty.columns == frame.columns


def test_missing_credentials_are_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Missing Alpaca credentials"):
        us.credentials()
    (tmp_path / ".env").write_text('ALPACA_API_KEY="a"\nALPACA_SECRET_KEY=b\n')
    assert us.credentials() == {"APCA-API-KEY-ID": "a", "APCA-API-SECRET-KEY": "b"}

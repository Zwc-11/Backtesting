"""Live paper desk: feed parsing, minute bars, quote fills, funding, restarts."""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from lab_helpers import DAY, bar, book, instrument, universe
from test_lab_execution import make_runtime

from xasset.lab.bars import read_lab_bars
from xasset.lab.live.aggregator import Aggregator
from xasset.lab.live.broker import FundingExposure, QuoteBroker
from xasset.lab.live.desk import BookDesk, BookSpec, LiveLedger
from xasset.lab.live.events import MarketCache, Quote, Trade
from xasset.lab.live.feeds import BinanceSpotFeed, HyperliquidFeed
from xasset.lab.live.recorder import BarRecorder
from xasset.lab.portfolio import Position
from xasset.lab.runtime import Runtime
from xasset.lab.strategy import Guard
from xasset.lab.universe import CostClass

S = timedelta(seconds=1)
MS = timedelta(milliseconds=1)


class Sink:
    def __init__(self) -> None:
        self.trades: list[Trade] = []
        self.quotes: list[Quote] = []
        self.connected: list[list[str]] = []

    def feed_connected(self, feed: str, symbols: list[str]) -> None:
        self.connected.append(symbols)

    def feed_disconnected(self, feed: str, symbols: list[str], error: str | None) -> None:
        pass

    def on_trade(self, feed: str, trade: Trade) -> None:
        self.trades.append(trade)

    def on_quote(self, feed: str, quote: Quote) -> None:
        self.quotes.append(quote)


def test_binance_parser_maps_aggressor_and_drops_duplicates() -> None:
    feed = BinanceSpotFeed({"BTCUSDT": "BTC"}, lambda: DAY)
    sink = Sink()
    trade = {"e": "aggTrade", "s": "BTCUSDT", "a": 7, "p": "100.5", "q": "2", "T": 1, "m": True}
    feed.parse(json.dumps({"stream": "btcusdt@aggTrade", "data": trade}), DAY, sink)
    feed.parse(json.dumps({"stream": "btcusdt@aggTrade", "data": trade}), DAY, sink)
    buyer = {**trade, "a": 8, "m": False}
    feed.parse(json.dumps({"stream": "btcusdt@aggTrade", "data": buyer}), DAY, sink)
    assert [t.side for t in sink.trades] == [-1, 1]  # maker buyer => seller initiated
    assert sink.trades[0].notional == pytest.approx(201.0)
    book_ticker = {"u": 5, "s": "BTCUSDT", "b": "100.4", "B": "1", "a": "100.6", "A": "3"}
    feed.parse(json.dumps({"stream": "btcusdt@bookTicker", "data": book_ticker}), DAY, sink)
    feed.parse(json.dumps({"stream": "btcusdt@bookTicker", "data": book_ticker}), DAY, sink)
    assert len(sink.quotes) == 1 and sink.quotes[0].mid == pytest.approx(100.5)
    other = {**trade, "s": "ETHUSDT", "a": 99}
    feed.parse(json.dumps({"stream": "ethusdt@aggTrade", "data": other}), DAY, sink)
    assert len(sink.trades) == 2
    assert "btcusdt@aggTrade/btcusdt@bookTicker" in feed.url()


def test_hyperliquid_parser_waits_for_both_subscriptions() -> None:
    feed = HyperliquidFeed({"ETH": "ETH-PERP"}, lambda: DAY)
    feed.ready = {"ETH": set()}
    sink = Sink()

    def response(kind: str) -> str:
        return json.dumps(
            {
                "channel": "subscriptionResponse",
                "data": {"method": "subscribe", "subscription": {"type": kind, "coin": "ETH"}},
            }
        )

    feed.parse(response("trades"), DAY, sink)
    assert sink.connected == []
    feed.parse(response("bbo"), DAY, sink)
    assert sink.connected == [["ETH-PERP"]]
    trades = [
        {"coin": "ETH", "side": "B", "px": "2484.9", "sz": "0.5", "time": 1000, "tid": 1},
        {"coin": "ETH", "side": "A", "px": "2484.8", "sz": "1", "time": 1001, "tid": 2},
    ]
    feed.parse(json.dumps({"channel": "trades", "data": trades}), DAY, sink)
    feed.parse(json.dumps({"channel": "trades", "data": trades[:1]}), DAY, sink)
    assert [t.side for t in sink.trades] == [1, -1]
    bbo = {"coin": "ETH", "time": 1002, "bbo": [{"px": "2484.8", "sz": "3", "n": 2}, None]}
    feed.parse(json.dumps({"channel": "bbo", "data": bbo}), DAY, sink)
    assert not sink.quotes[0].valid  # one-sided book is not executable


def quote(symbol: str, at: datetime, bid: float, ask: float) -> Quote:
    return Quote(symbol, bid, ask, 1.0, 1.0, at)


def trade(symbol: str, at: datetime, price: float, qty: float, side: int) -> Trade:
    return Trade(symbol, price, qty, side, at, at + 50 * MS)


def test_aggregator_matches_kline_conventions_and_counts_late_prints() -> None:
    cache = MarketCache()
    agg = Aggregator({"X": "paper-test"}, cache, timedelta(seconds=5), DAY)
    agg.connected(["X"], DAY - 10 * S)
    agg.on_quote(quote("X", DAY - 2 * S, 99.9, 100.1))
    agg.on_trade(trade("X", DAY + 5 * S, 100.0, 1, 1))
    agg.on_quote(quote("X", DAY + 10 * S, 100.4, 100.6))
    agg.on_trade(trade("X", DAY + 20 * S, 101.0, 2, -1))
    agg.on_trade(trade("X", DAY + 30 * S, 99.0, 1, 1))
    agg.on_quote(quote("X", DAY + 40 * S, 99.0, 99.2))
    agg.on_quote(quote("X", DAY + 58 * S, 99.0, 99.2))
    agg.on_trade(trade("X", DAY + 59 * S, 100.5, 1, -1))
    end = DAY + timedelta(minutes=1)
    agg.on_trade(trade("X", end + 100 * MS, 120.0, 1, 1))  # next minute
    assert agg.due(end + S, timedelta(seconds=1.5)) is None
    assert agg.due(end + 2 * S, timedelta(seconds=1.5)) == end
    [first] = agg.close(end, end + 2 * S)
    assert (first.open, first.high, first.low, first.close) == (100.0, 101.0, 99.0, 100.5)
    assert first.volume == 5 and first.trades == 4
    assert first.notional == pytest.approx(100 + 202 + 99 + 100.5)
    assert first.buy_notional == pytest.approx(199.0)
    assert first.sell_notional == pytest.approx(302.5)
    assert first.unclassified_notional == 0.0
    assert first.mid_open == pytest.approx(100.0)  # prevailing at the minute start
    assert first.mid_high == pytest.approx(100.5) and first.mid_low == pytest.approx(99.1)
    assert first.mid_close == pytest.approx(99.1)
    assert first.quote_age == pytest.approx(2.0)  # last quote at :58, bar ends at :60
    # One sample per second while the prevailing quote is at most 5 s old:
    # :00-:03 (pre-minute quote), :10-:15, :40-:45 and :58-:59.
    assert first.spread_samples is not None and len(first.spread_samples) == 18
    assert first.spread_samples[0] == pytest.approx(0.2 / 100.0)
    assert first.mid_close is not None and first.available_at == end + 2 * S
    # A print for the closed minute arriving now is late and changes nothing.
    assert not agg.on_trade(trade("X", end - S, 500.0, 1, 1))
    assert agg.health["X"].late_prints == 1
    # The second minute: one trade at 120, quotes stale by its end -> no midpoints.
    second_end = end + timedelta(minutes=1)
    [second] = agg.close(second_end, second_end + 2 * S)
    assert second.close == 120.0 and second.mid_close is None
    # A minute without trades repeats the previous close with zero volume (kline rule).
    third_end = second_end + timedelta(minutes=1)
    [third] = agg.close(third_end, third_end + 2 * S)
    assert (third.open, third.close, third.volume, third.notional) == (120.0, 120.0, 0.0, 0.0)
    assert third.buy_notional == 0.0 and third.sell_notional == 0.0


def test_aggregator_never_emits_partially_observed_minutes() -> None:
    cache = MarketCache()
    agg = Aggregator({"X": "paper-test"}, cache, timedelta(seconds=5), DAY)
    agg.connected(["X"], DAY + 30 * S)  # connected mid-minute
    end = DAY + timedelta(minutes=1)
    assert agg.close(end, end + 2 * S) == []
    second = end + timedelta(minutes=1)
    agg.on_trade(trade("X", end + S, 10.0, 1, 1))
    assert len(agg.close(second, second + 2 * S)) == 1
    agg.disconnected(["X"], second + 10 * S)
    agg.connected(["X"], second + 20 * S)
    third = second + timedelta(minutes=1)
    assert agg.close(third, third + 2 * S) == []
    assert agg.health["X"].skipped == 2
    # Prints from before the reconnection (snapshots) are ignored.
    assert not agg.on_trade(trade("X", second + 15 * S, 10.0, 1, 1))


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def quote_runtime(
    plan: dict, impact_bps: float = 1.0, fee_bps: float = 0.0, perp: bool = False
) -> tuple[Runtime, QuoteBroker, MarketCache, Clock]:
    costs = CostClass(
        half_spread_bps=5, impact_bps=impact_bps, fee_bps=fee_bps, evidence="quote test costs"
    )
    runtime = make_runtime(plan, costs=costs, perp=perp)
    cache = MarketCache(keep=timedelta(hours=1))
    clock = Clock(DAY)
    broker = QuoteBroker(runtime.accounting, cache, "mid", clock, lambda: None)
    broker.on_cancel = runtime.cancelled
    runtime.broker = broker
    return runtime, broker, cache, clock


def push(broker: QuoteBroker, cache: MarketCache, item: Quote) -> None:
    cache.add_quote(item)
    broker.on_quote(item)


def step(runtime: Runtime, clock: Clock, minute: int, symbol: str, price: float) -> datetime:
    end = DAY + timedelta(minutes=minute + 1)
    clock.now = end + timedelta(milliseconds=1300)
    runtime.step(end, [bar(symbol, end, price, price, price, price)])  # available end + 1 s
    return end


def test_entry_fills_at_the_ask_after_latency_and_stop_exits_at_next_bid() -> None:
    runtime, broker, cache, clock = quote_runtime(
        {"symbol": "BTC", "arm_at": 1, "stop": 99.0, "sigma10": 0.02}
    )
    step(runtime, clock, 0, "BTC", 100.0)
    step(runtime, clock, 1, "BTC", 100.0)  # arms
    end = DAY + timedelta(minutes=3)
    push(broker, cache, quote("BTC", end + S, 100.0, 100.1))
    step(runtime, clock, 2, "BTC", 100.0)  # confirms and submits at end + 1.3 s
    assert len(broker.entries) == 1
    order = broker.entries[0]
    # Decision = arrival (end + 1 s) + 250 ms; transport adds 150 ms after the later clock.
    assert order.eligible == end + timedelta(milliseconds=1450)
    push(broker, cache, quote("BTC", end + timedelta(milliseconds=1500), 100.2, 100.3))
    broker.process(end + timedelta(milliseconds=1600))
    position = runtime.portfolio.positions["BTC"]
    assert position.entry_price == pytest.approx(100.1 * math.exp(1e-4))  # prevailing ask
    assert position.entry_time == end + timedelta(milliseconds=1450)
    assert position.target == pytest.approx(
        position.entry_price * math.exp(2 * math.log(position.entry_price / 99.0))
    )
    t = end + 10 * S
    push(broker, cache, quote("BTC", t, 98.95, 99.05))  # bid at/below the stop: trigger
    push(broker, cache, quote("BTC", t + 100 * MS, 98.8, 98.9))
    push(broker, cache, quote("BTC", t + 200 * MS, 98.5, 98.6))
    broker.process(t + 300 * MS)
    [record] = runtime.ledger.trades
    assert record.exit_reason == "stop" and record.execution_basis == "quote"
    assert record.exit_price == pytest.approx(98.8 * math.exp(-1e-4))  # quote at t + 150 ms
    assert record.exit_time == t + 150 * MS
    assert "BTC" not in runtime.portfolio.positions


def test_target_and_time_exits_use_executable_quotes() -> None:
    runtime, broker, cache, clock = quote_runtime(
        {"symbol": "BTC", "arm_at": 1, "stop": 99.0, "sigma10": 0.02, "time_exit": 5},
        impact_bps=0.0,
    )
    step(runtime, clock, 0, "BTC", 100.0)
    step(runtime, clock, 1, "BTC", 100.0)
    end = DAY + timedelta(minutes=3)
    push(broker, cache, quote("BTC", end + S, 99.9, 100.0))
    step(runtime, clock, 2, "BTC", 100.0)
    broker.process(end + 2 * S)
    position = runtime.portfolio.positions["BTC"]
    assert position.target is not None
    # The mid passes the target but the bid does not: no exit yet.
    push(broker, cache, quote("BTC", end + 5 * S, position.target - 0.01, position.target + 0.5))
    broker.process(end + 6 * S)
    assert "BTC" in runtime.portfolio.positions
    # Time exit at entry + 5 minutes, at the bid prevailing 150 ms later.
    deadline = position.deadline
    push(broker, cache, quote("BTC", deadline - S, 100.4, 100.5))
    broker.process(deadline + 200 * MS)
    [record] = runtime.ledger.trades
    assert record.exit_reason == "time" and record.exit_price == pytest.approx(100.4)
    assert record.exit_time == deadline + 150 * MS


def test_invalidation_before_fill_cancels_the_order() -> None:
    plan = {
        "symbol": "BTC",
        "arm_at": 1,
        "stop": 99.0,
        "sigma10": 0.02,
        "guards": [Guard("ETH", "below", 50.0)],
    }
    runtime, broker, cache, clock = quote_runtime(plan)
    step(runtime, clock, 0, "BTC", 100.0)
    step(runtime, clock, 1, "BTC", 100.0)
    end = DAY + timedelta(minutes=3)
    push(broker, cache, quote("BTC", end + S, 100.0, 100.1))
    # The guard asset trades through its level after the signal bar, before submission.
    push(broker, cache, quote("ETH", end + 500 * MS, 49.0, 49.1))
    step(runtime, clock, 2, "BTC", 100.0)
    assert broker.entries == []
    cancelled = [o for o in runtime.ledger.orders if o["status"] == "cancelled"]
    assert cancelled and cancelled[0]["reason"] == "invalidated before entry: ETH"
    # The own stop is a guard too: a later setup whose stop trades before the fill.
    runtime2, broker2, cache2, clock2 = quote_runtime(
        {"symbol": "BTC", "arm_at": 1, "stop": 99.0, "sigma10": 0.02}
    )
    step(runtime2, clock2, 0, "BTC", 100.0)
    step(runtime2, clock2, 1, "BTC", 100.0)
    push(broker2, cache2, quote("BTC", end + S, 100.0, 100.1))
    step(runtime2, clock2, 2, "BTC", 100.0)
    push(broker2, cache2, quote("BTC", end + timedelta(milliseconds=1420), 98.8, 98.9))
    broker2.process(end + 2 * S)
    assert "BTC" not in runtime2.portfolio.positions


def test_funding_applies_to_positions_closed_after_the_funding_time() -> None:
    runtime, broker, cache, clock = quote_runtime(
        {"symbol": "PERP", "arm_at": 1, "stop": 101.0, "sigma10": 0.02, "direction": -1},
        impact_bps=0.0,
        perp=True,
    )
    step(runtime, clock, 0, "PERP", 100.0)
    step(runtime, clock, 1, "PERP", 100.0)
    end = DAY + timedelta(minutes=3)
    push(broker, cache, quote("PERP", end + S, 100.0, 100.1))
    step(runtime, clock, 2, "PERP", 100.0)
    broker.process(end + 2 * S)
    position = runtime.portfolio.positions["PERP"]
    funding_time = end + timedelta(minutes=1)
    push(broker, cache, quote("PERP", funding_time - S, 99.9, 100.1))
    [exposure] = broker.funding_snapshot(funding_time)
    assert exposure == FundingExposure("PERP", position.setup, -1, position.qty, 100.0)
    # The position stops out right after the funding time, before the rate is published.
    push(broker, cache, quote("PERP", funding_time + S, 101.0, 101.2))
    broker.process(funding_time + 2 * S)
    [record] = runtime.ledger.trades
    cash = runtime.portfolio.cash
    broker.apply_funding(exposure, 0.0001, funding_time)  # positive: shorts receive
    received = position.qty * 100.0 * 0.0001
    assert runtime.portfolio.cash == pytest.approx(cash + received)
    assert record.funding == pytest.approx(received)
    assert record.net == pytest.approx(record.gross - record.fees + received)


def test_book_state_survives_a_restart(tmp_path: Path) -> None:
    uni = universe(["BTC", "ETH"])
    bk = book(["h01"])
    spec = BookSpec(book=Path("x.yaml"), basis="mid", warmup="recorded")
    cache = MarketCache()
    first = BookDesk(spec, bk, uni, tmp_path / "book", cache, lambda: DAY, "a-")
    first.go_live(DAY)
    runtime = first.runtime
    assert isinstance(runtime.ledger, LiveLedger)
    runtime.portfolio.cash = 90_000.0
    runtime.portfolio.positions["BTC"] = position = Position(
        symbol="BTC",
        strategy="h01",
        setup="a-h01-000001",
        direction=1,
        qty=100.0,
        kind="spot",
        entry_time=DAY,
        entry_price=100.0,
        entry_fee=0.0,
        entry_notional=10_000.0,
        stop=99.0,
        target=102.0,
        deadline=DAY + timedelta(hours=1),
        initial_risk=100.0,
        risk_distance=0.01,
        signal_time=DAY,
        decided_at=DAY,
    )
    runtime.portfolio.marks["BTC"] = 101.0
    first.save_state(DAY + S)
    second = BookDesk(spec, bk, uni, tmp_path / "book", cache, lambda: DAY, "b-")
    second.go_live(DAY + timedelta(minutes=5))
    restored = second.runtime.portfolio
    assert restored.cash == 90_000.0
    assert restored.positions["BTC"] == position
    assert restored.nav() == pytest.approx(90_000.0 + 100 * 101.0)
    assert second.restored is not None and second.restored["positions"] == 1


def test_recorder_merges_flushes_into_daily_files(tmp_path: Path) -> None:
    recorder = BarRecorder(tmp_path, {"BTC": "BTCUSDT"})
    first = bar("BTC", DAY + timedelta(minutes=1), 1, 1, 1, 1)
    first.source = "paper-binance"
    recorder.add([first])
    recorder.flush()
    second = bar("BTC", DAY + timedelta(minutes=2), 2, 2, 2, 2)
    second.source = "paper-binance"
    recorder.add([second, first])
    recorder.flush()
    frame = read_lab_bars(tmp_path, "paper-binance", "BTCUSDT", DAY, DAY + timedelta(days=1))
    assert frame["close"].to_list() == [1.0, 2.0]
    assert frame.schema == pl.read_parquet(next((tmp_path / "lab").rglob("*.parquet"))).schema
    assert recorder.buffer == {} and recorder.written == 3


def test_instrument_helper_supports_perps() -> None:
    perp = instrument("PERP", kind="perp", shortable=True, breadth_member=False)
    assert perp.archive_symbol == "PERP"

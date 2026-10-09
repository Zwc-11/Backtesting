from dataclasses import replace
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from pydantic import ValidationError
from test_native_engine import PARAMS, frame, instrument, profile, request

from xasset.config import Instrument
from xasset.normalize.calendars import calendar
from xasset.research.contracts import Fold
from xasset.research.engine import validate_design
from xasset.research.execution import simulate
from xasset.research.experiment import ScheduledEvent
from xasset.research.session_signals import generate
from xasset.research.strategy import quote_aggregates, signals


def equity(symbol="FOLLOWER"):
    return Instrument(
        id=symbol,
        asset_class="equity",
        tier="A",
        venue="XNYS",
        calendar="XNYS",
        session="regular",
        yahoo_symbol=symbol,
    )


def session_request(at, bars, *, anchor="previous_close", driver=None):
    original = request()
    spec = original.experiment.model_copy(
        update={
            "strategy": "session_open",
            "start": at - timedelta(days=10),
            "end": at + timedelta(days=10),
        }
    )
    return replace(
        original,
        experiment=spec,
        bars=bars,
        instruments=[driver or instrument("DRIVER"), equity()],
        data_start=at - timedelta(days=5),
        data_end=at + timedelta(hours=6),
    ), {**PARAMS, "anchor": anchor, "hold_minutes": 390}


def test_weekend_crypto_is_known_at_monday_open_and_causal():
    monday = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)
    friday = datetime(2026, 9, 11, 20, tzinfo=UTC)
    count = int((monday - friday).total_seconds() // 60) + 1
    bars = frame(
        [(100 + i * 0.001, 101 + i * 0.001, 99 + i * 0.001, 100 + i * 0.001) for i in range(count)],
        "DRIVER",
        friday - timedelta(minutes=1),
    )
    req, params = session_request(monday, bars)
    assert generate(req, params) == {monday: monday}
    assert generate(req, params, monday - timedelta(minutes=1)) == {}
    future = frame([(1000, 2000, 500, 1500)] * 10, "DRIVER", monday)
    assert generate(replace(req, bars=pl.concat([bars, future])), params) == {monday: monday}
    # One absent weekend minute invalidates the complete observation window.
    missing = bars.filter(pl.col("ts_end") != friday + timedelta(hours=1))
    assert generate(replace(req, bars=missing), params) == {}


@pytest.mark.parametrize("day,hour", [("2026-03-06", 14), ("2026-03-09", 13)])
def test_session_open_tracks_new_york_dst(day, hour):
    at = datetime.fromisoformat(day).replace(hour=hour, minute=30, tzinfo=UTC)
    previous = (
        calendar("XNYS").session_close(calendar("XNYS").previous_session(day)).to_pydatetime()
    )
    count = int((at - previous).total_seconds() // 60) + 1
    bars = frame([(100, 102, 99, 101)] * count, "DRIVER", previous - timedelta(minutes=1))
    bars = bars.with_columns(
        pl.when(pl.col("ts_end") == previous)
        .then(pl.lit(100.0))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    req, params = session_request(at, bars)
    assert at in generate(req, params)


def quote_frame(prices, start, symbol="DRIVER"):
    bars = frame(prices, symbol, start).with_columns(
        pl.lit("cfd").alias("asset_class"), pl.lit(None, dtype=pl.Float64).alias("volume")
    )
    return bars.with_columns(
        [
            (pl.col(name) + offset).alias(f"{side}_{name}")
            for side, offset in [("bid", -0.01), ("ask", 0.01)]
            for name in ("open", "high", "low", "close")
        ]
    )


def quote_instrument(cal=None):
    return instrument("DRIVER").model_copy(
        update={"asset_class": "cfd", "calendar": cal, "currency": "JPY"}
    )


def test_regional_session_uses_only_information_available_at_us_open():
    us_open = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)
    cal = calendar("XETR")
    opened = cal.session_open("2026-09-14").to_pydatetime()
    count = int((us_open - opened).total_seconds() // 60)
    bars = quote_frame([(100, 102, 99, 101)] * count, opened)
    req, params = session_request(
        us_open, bars, anchor="regional_session", driver=quote_instrument("XETR")
    )
    assert generate(req, params) == {us_open: us_open}
    future = quote_frame([(500, 900, 400, 800)] * 60, us_open)
    assert generate(replace(req, bars=pl.concat([bars, future])), params) == {us_open: us_open}
    assert generate(req, params, us_open - timedelta(minutes=1)) == {}


def test_event_observation_waits_for_complete_post_release_window():
    at = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    event = ScheduledEvent(
        id="fixture",
        kind="eia_petroleum",
        at=at,
        known_at=at - timedelta(days=1),
        reference="Synthetic published calendar",
    )
    original = request()
    bars = quote_frame(
        [(100 + i, 102 + i, 99 + i, 101 + i) for i in range(10)], at - timedelta(minutes=1)
    )
    spec = original.experiment.model_copy(update={"strategy": "event_response", "events": [event]})
    req = replace(original, experiment=spec, bars=bars, instruments=[quote_instrument(), equity()])
    params = {**PARAMS, "observation_minutes": 5}
    expected = at + timedelta(minutes=5)
    assert generate(req, params) == {expected: expected}
    assert generate(req, params, expected - timedelta(minutes=1)) == {}
    future_changed = bars.with_columns(
        pl.when(pl.col("ts_end") > expected)
        .then(pl.lit(999.0))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    assert generate(replace(req, bars=future_changed), params) == {expected: expected}
    missing = bars.filter(pl.col("ts_end") != at + timedelta(minutes=3))
    assert generate(replace(req, bars=missing), params) == {}
    with pytest.raises(ValidationError, match="known before"):
        ScheduledEvent.model_validate({**event.model_dump(), "known_at": at + timedelta(minutes=1)})


def test_quote_driver_aggregates_need_all_minutes_and_both_sides():
    start = datetime(2026, 1, 2, tzinfo=UTC)
    bars = quote_frame([(100 + i, 102 + i, 99 + i, 101 + i) for i in range(10)], start)
    aggregated = quote_aggregates(bars, "5m")
    assert aggregated.height == 2
    assert aggregated["open"].to_list() == [100, 105]
    assert aggregated["close"].to_list() == [105, 110]
    assert signals(bars, quote_instrument(), "5m", PARAMS) == {
        start + timedelta(minutes=10): start + timedelta(minutes=10)
    }
    broken = bars.filter(pl.col("ts_end") != start + timedelta(minutes=3))
    assert quote_aggregates(broken, "5m").height == 1
    unquoted = bars.with_columns(pl.lit(None, dtype=pl.Float64).alias("ask_close"))
    assert quote_aggregates(unquoted, "5m").is_empty()


def test_driver_currency_needs_no_fx_conversion_but_follower_does():
    original = request()
    driver = original.instruments[0].model_copy(update={"currency": "JPY", "asset_class": "cfd"})
    validate_design(original.experiment, [driver, original.instruments[1]], original.costs)
    with pytest.raises(ValueError, match="common quote"):
        validate_design(
            original.experiment,
            [driver, original.instruments[1].model_copy(update={"currency": "JPY"})],
            original.costs,
        )


def test_half_day_exit_uses_scheduled_close_not_next_session():
    cal = calendar("XNYS")
    opened = cal.session_open("2026-11-27").to_pydatetime()
    closed = cal.session_close("2026-11-27").to_pydatetime()
    count = int((closed - opened).total_seconds() // 60)
    assert count == 210
    bars = frame([(100, 102, 99, 101)] * count, start=opened).with_columns(
        pl.lit("equity").alias("asset_class")
    )
    current = Fold(
        id="halfday",
        train_start=opened - timedelta(days=2),
        train_end=opened - timedelta(days=1),
        test_start=opened,
        test_end=closed,
        parameters={**PARAMS, "hold_minutes": 390},
    )
    result = simulate(bars, equity(), current, {opened: opened}, profile(), 1, 1000, 0.5)
    (trade,) = result.trades
    assert trade.exit_reason == "session_close"
    assert trade.exit_at == closed
    assert trade.exit_price == 101
    assert result.ending_cash == 1005
    with pytest.raises(ValueError, match="Unclosed position"):
        simulate(bars.head(count - 1), equity(), current, {opened: opened}, profile(), 1, 1000, 0.5)


def test_out_of_session_follower_bars_cannot_fill():
    start = datetime(2026, 9, 14, 12, tzinfo=UTC)  # premarket, not a regular-session fill
    bars = frame([(100, 102, 99, 101)] * 10, start=start).with_columns(
        pl.lit("equity").alias("asset_class")
    )
    current = Fold(
        id="premarket",
        train_start=start - timedelta(days=2),
        train_end=start - timedelta(days=1),
        test_start=start,
        test_end=start + timedelta(hours=1),
        parameters=PARAMS,
    )
    result = simulate(bars, equity(), current, {start: start}, profile(), 1, 1000, 0.5)
    assert not result.trades and result.ending_cash == 1000

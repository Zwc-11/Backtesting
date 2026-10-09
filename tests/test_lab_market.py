import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from lab_helpers import DAY, bar, book, universe

from xasset.lab.calibration import Calibration, SessionStatistic
from xasset.lab.market import Market, Tape, returns_array, rolling_valid_sum, tape_features
from xasset.lab.sessions import Sessions


def test_calibration_hides_current_session_and_uses_neighbourhood() -> None:
    cal = Calibration(width=60, sessions=2, half_window=2, minimum=3)
    cal.begin("d1")
    for minute in range(60):
        cal.record(minute, float(minute))
    assert cal.quantile(10, 0.5) is None  # no completed session yet
    cal.begin("d2")
    for minute in range(60):
        cal.record(minute, 1000.0)  # current session must stay invisible
    assert cal.quantile(10, 0.5) is None  # only one completed session
    cal.begin("d3")
    # Reference: sessions d1 and d2, minutes 8..12.
    values = sorted([8.0, 9.0, 10.0, 11.0, 12.0] + [1000.0] * 5)
    assert cal.quantile(10, 0.5) == pytest.approx(float(np.quantile(values, 0.5)))
    center = np.median(values)
    assert cal.scale(10) == pytest.approx(1.4826 * np.median(np.abs(np.array(values) - center)))
    # Truncated neighbourhood at the session edge still requires the minimum count.
    assert Calibration(width=60, sessions=1, half_window=0, minimum=3).reference(0) is None


def test_calibration_minimum_observations() -> None:
    cal = Calibration(width=10, sessions=1, half_window=1, minimum=4)
    cal.begin("a")
    cal.record(5, 1.0)
    cal.record(6, 2.0)
    cal.begin("b")
    assert cal.median(5) is None  # two valid values < minimum four


def test_session_statistic_is_prior_only() -> None:
    stat = SessionStatistic(sessions=2)
    stat.record("a", 1.0)
    assert stat.median() is None
    stat.close("a")
    stat.record("b", 3.0)
    assert stat.median() is None
    stat.close("b")
    assert stat.median() == 2.0


def test_returns_never_span_gaps_or_sessions() -> None:
    tape = Tape(10)
    for index, price in [(0, 100.0), (1, 101.0), (2, 102.0), (4, 104.0)]:
        tape.put(index, bar("X", DAY, price, price, price, price), "trade")
    assert tape.ret(2, 2) == pytest.approx(math.log(102 / 100))
    assert tape.ret(4, 1) is None  # minute 3 missing
    assert tape.ret(0, 1) is None  # would bridge the session boundary


def test_flow_requires_coverage_and_positive_notional() -> None:
    tape = Tape(5)
    tape.put(0, bar("X", DAY, 1, 1, 1, 1, notional=100, buy=60, sell=40), "trade")
    tape.put(1, bar("X", DAY, 1, 1, 1, 1, notional=100, buy=50, sell=44), "trade")
    assert tape.flow(0, 0) == (60, 40, 100)
    # 194 classified of 200 = 97%: passes; drop to 90% fails.
    assert tape.flow(0, 1) is not None
    tape.put(2, bar("X", DAY, 1, 1, 1, 1, notional=100, buy=30, sell=50), "trade")
    assert tape.flow(0, 2) is None
    tape.put(3, bar("X", DAY, 1, 1, 1, 1, notional=0, buy=0, sell=0), "trade")
    assert tape.flow(3, 3) is None  # imbalance undefined at zero notional
    unlabelled = bar("X", DAY, 1, 1, 1, 1, notional=100)
    tape.put(4, unlabelled, "trade")
    assert tape.flow(4, 4) is None  # unknown flow is never zero flow


def test_vectorized_features_match_online_definitions() -> None:
    rng = np.random.default_rng(7)
    tape = Tape(120)
    price = 100.0
    for index in range(120):
        if index in {17, 18, 60}:
            continue  # gaps
        price *= math.exp(rng.normal(0, 0.002))
        notional = float(rng.uniform(50, 150))
        buy = notional * float(rng.uniform(0.3, 0.7))
        sell = (notional - buy) * (0.5 if index == 40 else 1.0)  # one poorly covered minute
        tape.put(
            index,
            bar("X", DAY, price, price * 1.001, price * 0.999, price, notional, buy, sell),
            "trade",
        )
    features = tape_features(tape)
    for h in (1, 2, 3, 5, 10):
        for i in range(120):
            online = tape.ret(i, h)
            value = features[f"R{h}"][i]
            assert (online is None) == (not np.isfinite(value))
            if online is not None:
                assert value == pytest.approx(online)
    for i in range(120):
        online_n10 = tape.sum_notional(i - 9, i)
        assert (online_n10 is None) == (not np.isfinite(features["N10"][i]))
        flow = tape.flow(i - 9, i)
        assert (flow is None) == (not np.isfinite(features["B10"][i]))
        if flow is not None:
            assert features["B10"][i] == pytest.approx(flow[0])
        if i % 2 == 1:
            block = tape.flow(i - 1, i)
            q = features["Q2"][i]
            assert (block is None) == (not np.isfinite(q))
            if block is not None:
                assert q == pytest.approx(block[0] - block[1])
        else:
            assert not np.isfinite(features["Q2"][i])


def test_rolling_helpers() -> None:
    values = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
    assert np.allclose(rolling_valid_sum(values, 2), [np.nan, 3, np.nan, np.nan, 9], equal_nan=True)
    lc = np.log(np.array([100.0, 110.0, 121.0]))
    assert returns_array(lc, 2)[2] == pytest.approx(math.log(1.21))


def test_residual_model_and_online_residual_are_frozen_per_session() -> None:
    uni = universe(["BTC", "ETH"], benchmark="BTC")
    bk = book(["h01"])
    market = Market(uni, bk, "trade")
    rng = np.random.default_rng(1)
    prices = {"BTC": 100.0, "ETH": 50.0}
    for day in range(6):
        start = DAY + timedelta(days=day)
        for minute in range(0, 1440, 1):
            end = start + timedelta(minutes=minute + 1)
            assert market.advance(end)
            shock = rng.normal(0, 0.001)
            prices["BTC"] *= math.exp(shock)
            prices["ETH"] *= math.exp(1.5 * shock + rng.normal(0, 0.0005))
            for symbol, price in prices.items():
                market.add(bar(symbol, end, price, price, price, price))
    model = market.residual["ETH"]
    assert model is not None and model.factors == ("BTC",)
    assert model.coefficients[0] == pytest.approx(1.5, abs=0.1)
    assert market.residual["BTC"] is None  # the benchmark has no factor of its own
    online = market.residual_sum("ETH", market.minute, 10)
    expected = 0.0
    for u in range(market.minute - 9, market.minute + 1):
        y = market.tapes["ETH"].ret(u, 1)
        x = market.tapes["BTC"].ret(u, 1)
        assert y is not None and x is not None
        expected += y - model.intercept - model.coefficients[0] * x
    assert online == pytest.approx(expected)


def test_sessions_follow_exchange_calendar_and_daylight_saving() -> None:
    nyse = Sessions("XNYS")
    winter = nyse.for_date(date(2026, 3, 6))
    summer = nyse.for_date(date(2026, 3, 9))  # after the US switch to daylight time
    assert winter is not None and winter.open.hour == 14 and winter.open.minute == 30
    assert summer is not None and summer.open.hour == 13 and summer.open.minute == 30
    assert nyse.for_date(date(2026, 3, 7)) is None  # Saturday
    half = nyse.for_date(date(2026, 11, 27))  # day after Thanksgiving closes early
    assert half is not None and half.minutes == 210
    located = nyse.locate(datetime(2026, 3, 9, 13, 30, tzinfo=UTC))
    assert located is not None and located[1] == 0
    assert nyse.locate(datetime(2026, 3, 9, 13, 29, tzinfo=UTC)) is None
    utc_days = Sessions(None)
    found = utc_days.locate(datetime(2026, 3, 9, 23, 59, tzinfo=UTC))
    assert found is not None and found[1] == 1439

from datetime import timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from xasset.features.returns import trailing_features
from xasset.normalize.adjust import Split, adjust_splits
from xasset.normalize.futures_roll import Roll, continuous
from xasset.store.schema import BAR_SCHEMA


def test_split_respects_known_time_and_effective_time(bars):
    first, switch, last = bars["ts_end"][0], bars["ts_end"][20], bars["ts_end"][-1]
    action = Split("TEST", switch, switch + timedelta(minutes=10), 2.0)
    assert_frame_equal(adjust_splits(bars, [action], switch), bars.head(21))
    adjusted = adjust_splits(bars, [action], last)
    assert adjusted["close"][0] == bars["close"][0] / 2
    assert adjusted["volume"][0] == 20
    assert adjusted["close"][21] == bars["close"][21]
    assert "unadjusted" not in adjusted["flags"][0]
    future = Split("TEST", last + timedelta(days=1), first, 5)
    assert_frame_equal(adjust_splits(bars, [future], last), bars)
    with pytest.raises(ValueError, match="twice"):
        adjust_splits(adjusted, [action], last)


def contract(bars, symbol, offset):
    return bars.with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.lit("futures").alias("asset_class"),
        *[(pl.col(name) + offset).alias(name) for name in ("open", "high", "low", "close")],
    )


def test_roll_contract_identity_boundaries_and_no_synthetic_return(bars):
    begin, change, last = (
        bars["ts_end"][0] - timedelta(minutes=1),
        bars["ts_end"][19],
        bars["ts_end"][-1],
    )
    contracts = {"ESU26": contract(bars, "ESU26", 0), "ESZ26": contract(bars, "ESZ26", 500)}
    schedule = [Roll("ESU26", begin, begin), Roll("ESZ26", change, begin)]
    rolled = continuous(contracts, schedule, "ES_CONT", last)
    assert rolled.height == 60
    assert rolled["contract_symbol"][19] == "ESU26"
    assert rolled["contract_symbol"][20] == "ESZ26"
    assert rolled["close"][19] == bars["close"][19]
    assert rolled["close"][20] == bars["close"][20] + 500
    features = trailing_features(rolled.select(BAR_SCHEMA.names()))
    assert features["return_1m"][20] is None
    # Appending later contracts cannot rewrite the already emitted history.
    earlier = continuous(contracts, schedule, "ES_CONT", change)
    assert_frame_equal(earlier, rolled.head(20))


def test_roll_rejects_unknown_contract_and_proxy_data(bars):
    begin, end = bars["ts_end"][0] - timedelta(minutes=1), bars["ts_end"][-1]
    with pytest.raises(ValueError, match="Missing contract"):
        continuous({}, [Roll("ESU26", begin, begin)], "ES", end)
    proxy = contract(bars, "ES_FRONT", 0).with_columns(pl.lit(["front_month_proxy"]).alias("flags"))
    with pytest.raises(ValueError, match="explicit contract"):
        continuous({"ES_FRONT": proxy}, [Roll("ES_FRONT", begin, begin)], "ES", end)
    with pytest.raises(ValueError, match="known no later"):
        Roll("ESU26", begin, begin + timedelta(days=1))

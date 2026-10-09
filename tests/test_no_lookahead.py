from datetime import timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from xasset.features.returns import trailing_features


@pytest.mark.parametrize("length", [5, 20, 21, 35, 59])
def test_truncating_future_does_not_change_features(bars, length):
    full = trailing_features(bars)
    truncated = trailing_features(bars.head(length))
    assert_frame_equal(full.head(length), truncated)


def test_replacing_future_prices_does_not_change_past_features(bars):
    cutoff = bars["ts_end"][30]
    changed = bars.with_columns(
        pl.when(pl.col("ts_end") > cutoff)
        .then(pl.col(name) * 20)
        .otherwise(pl.col(name))
        .alias(name)
        for name in ("open", "high", "low", "close")
    )
    assert_frame_equal(trailing_features(bars).head(31), trailing_features(changed).head(31))


def test_returns_known_answer_and_volatility_warmup(bars):
    features = trailing_features(bars, window=3)
    assert features["return_1m"][0] is None
    assert features["return_1m"][1] == pytest.approx(102 / 101 - 1)
    assert features["volatility"][:3].null_count() == 3
    assert features["volatility"][3] is not None


def test_gaps_and_instrument_boundaries_are_not_returns(bars):
    gap = pl.concat([bars.head(10), bars.slice(11)])
    result = trailing_features(gap)
    assert result["return_1m"][10] is None
    second = bars.with_columns(pl.lit("SECOND").alias("symbol"))
    mixed = trailing_features(pl.concat([bars, second]))
    assert mixed.group_by("symbol").first()["return_1m"].null_count() == 2
    # The same minute on the next day cannot become a one-minute return.
    shifted = bars.head(1).with_columns(pl.col("ts_end") + timedelta(days=1))
    assert trailing_features(pl.concat([bars, shifted]))["return_1m"][-1] is None


def test_source_switch_is_not_a_tradable_return(bars):
    mixed = pl.concat(
        [bars.head(20), bars.slice(20).with_columns(pl.lit("alpaca").alias("source"))]
    )
    assert trailing_features(mixed)["return_1m"][20] is None

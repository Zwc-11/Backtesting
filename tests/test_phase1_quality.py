from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from conftest import bars_at
from polars.testing import assert_frame_equal

from xasset.config import Instrument
from xasset.normalize.resample import resample
from xasset.qc.audit import audit_bars
from xasset.qc.checks import check_bars
from xasset.qc.reconcile import reconcile
from xasset.store.reader import load
from xasset.store.schema import QUOTE_COLUMNS
from xasset.store.writer import load_bars, merge_bars, writer_lock


def test_priority_keeps_both_sources_and_ignores_ingest_order(tmp_path, instrument, bars):
    item = instrument.model_copy(update={"sources": ["alpaca", "yahoo"], "alpaca_symbol": "TEST"})
    preferred = bars.with_columns(
        pl.lit("alpaca").alias("source"), (pl.col("close") + 0.1).alias("close")
    )
    with writer_lock(tmp_path):
        merge_bars(tmp_path, item, preferred)
        merge_bars(tmp_path, item, bars)
        merge_bars(tmp_path, item, bars.with_columns((pl.col("close") - 0.1).alias("close")))
    assert_frame_equal(load_bars(tmp_path, item), preferred)
    assert load_bars(tmp_path, item, "yahoo")["close"][0] == bars["close"][0] - 0.1


def test_legacy_partition_upgrades_without_losing_old_provider(tmp_path, instrument, bars):
    legacy = bars.drop(QUOTE_COLUMNS).with_columns(pl.col("volume").cast(pl.Int64))
    target = tmp_path / "bars/equity/TEST/2026-10.parquet"
    target.parent.mkdir(parents=True)
    legacy.write_parquet(target)
    item = instrument.model_copy(update={"sources": ["alpaca", "yahoo"], "alpaca_symbol": "TEST"})
    preferred = bars.with_columns(pl.lit("alpaca").alias("source"))
    with writer_lock(tmp_path):
        merge_bars(tmp_path, item, preferred)
    assert_frame_equal(load_bars(tmp_path, item, "yahoo"), bars)
    assert_frame_equal(load_bars(tmp_path, item), preferred)


def test_reconcile_agreement_disagreement_and_missing_overlap(bars):
    other = bars.with_columns(pl.lit("alpaca").alias("source"))
    assert reconcile(bars, other)["ok"]
    assert not reconcile(bars, other.head(1))["ok"]
    changed = other.with_columns((pl.col("high") * 2).alias("high"))
    result = reconcile(bars, changed)
    assert not result["ok"]
    assert result["disagreements"] == 60
    assert not reconcile(bars.head(10), other.tail(10))["ok"]
    with pytest.raises(ValueError, match="distinct sources"):
        reconcile(bars, bars)


def test_audit_reports_split_like_jump_and_zero_volume_runs(bars):
    changed = bars.with_columns(
        pl.when(pl.col("ts_end") >= bars["ts_end"][10])
        .then(pl.col(name) / 2)
        .otherwise(pl.col(name))
        .alias(name)
        for name in ("open", "high", "low", "close")
    ).with_columns(pl.lit(0.0).alias("volume"))
    report = audit_bars(changed)
    assert not report["ok"]
    assert any(row["kind"] == "zero_volume_run" for row in report["anomalies"])
    assert any(row.get("possible_split") for row in report["anomalies"])
    assert report["cross_source_verified"] is False


def test_reader_marks_missing_and_closed_minutes_without_imputing_prices(tmp_path, instrument):
    times = [datetime(2026, 10, 7, 13, 31, tzinfo=UTC), datetime(2026, 10, 7, 13, 33, tzinfo=UTC)]
    with writer_lock(tmp_path):
        merge_bars(tmp_path, instrument, bars_at(times))
    frame = load(tmp_path, [instrument], times[0] - timedelta(minutes=2), times[-1])
    assert frame["session_open"].to_list() == [False, True, True, True]
    assert frame["stale"].to_list() == [False, False, True, False]
    assert frame["close"][2] is None
    assert frame["age_seconds"][2] == 60


def test_unknown_futures_calendar_cannot_claim_missing_or_open(tmp_path, instrument):
    item = instrument.model_copy(
        update={"calendar": None, "session": "all", "asset_class": "futures"}
    )
    start = datetime(2026, 10, 7, tzinfo=UTC)
    frame = load(tmp_path, [item], start, start + timedelta(minutes=2))
    assert frame["session_open"].null_count() == 2
    assert frame["stale"].null_count() == 2


def test_resample_known_answer_and_gap_rejection(instrument, bars):
    grouped = resample(bars, instrument, "5m")
    assert grouped.height == 12
    assert grouped["ts_end"][0] == bars["ts_end"][4]
    assert grouped["open"][0] == bars["open"][0]
    assert grouped["close"][0] == bars["close"][4]
    assert grouped["volume"][0] == 50
    missing = pl.concat([bars.head(2), bars.slice(3)])
    assert resample(missing, instrument, "5m").height == 11
    assert resample(bars.head(4), instrument, "5m").is_empty()


def test_resampling_is_invariant_to_future_truncation(instrument, bars):
    whole = resample(bars, instrument, "5m")
    partial = resample(bars.head(23), instrument, "5m")
    assert_frame_equal(whole.head(4), partial)


def test_resampling_respects_half_day_and_lunch(instrument):
    from xasset.normalize.calendars import expected_bar_ends

    start, end = datetime(2025, 11, 28, tzinfo=UTC), datetime(2025, 11, 29, tzinfo=UTC)
    minute_bars = bars_at(sorted(expected_bar_ends("XNYS", start, end)))
    daily = resample(minute_bars, instrument, "1d")
    assert daily.height == 1
    assert daily["volume"][0] == 2100
    assert daily["ts_end"][0] == datetime(2025, 11, 28, 18, tzinfo=UTC)
    hk = instrument.model_copy(update={"calendar": "XHKG"})
    start, end = datetime(2026, 10, 7, tzinfo=UTC), datetime(2026, 10, 8, tzinfo=UTC)
    hourly = resample(bars_at(sorted(expected_bar_ends("XHKG", start, end))), hk, "1h")
    assert datetime(2026, 10, 7, 4, tzinfo=UTC) in hourly["ts_end"].to_list()
    assert datetime(2026, 10, 7, 5, tzinfo=UTC) not in hourly["ts_end"].to_list()


def test_resampling_does_not_mix_providers_inside_bucket(instrument, bars):
    mixed = pl.concat([bars.head(2), bars.slice(2).with_columns(pl.lit("alpaca").alias("source"))])
    assert resample(mixed, instrument, "5m").height == 11


def test_crypto_midnight_bar_belongs_to_previous_day():
    item = Instrument(
        id="BTC",
        asset_class="crypto",
        tier="A",
        session="all",
        venue="BINANCE",
        sources=["binance"],
        binance_symbol="BTCUSDT",
    )
    start = datetime(2026, 9, 1, 23, 56, tzinfo=UTC)
    bars = bars_at([start + timedelta(minutes=i) for i in range(5)], "BTC")
    output = resample(bars, item, "5m")
    assert output.height == 1
    assert output["ts_end"][0] == datetime(2026, 9, 2, tzinfo=UTC)


def test_nonfinite_volume_and_crossed_quotes_fail(bars):
    assert not check_bars(bars.with_columns(pl.lit(float("nan")).alias("volume"))).ok
    quoted = bars.with_columns(
        pl.lit(5.0 if name.startswith("bid") else 4.0).alias(name) for name in QUOTE_COLUMNS
    )
    assert "invalid bid/ask OHLC" in check_bars(quoted).errors


def test_alpaca_feed_change_cannot_silently_rewrite_store(tmp_path, instrument, bars):
    item = instrument.model_copy(update={"sources": ["alpaca"], "alpaca_symbol": "TEST"})
    iex = bars.with_columns(pl.lit("alpaca").alias("source"), pl.lit(["feed_iex"]).alias("flags"))
    sip = iex.with_columns(pl.lit(["feed_sip"]).alias("flags"))
    with writer_lock(tmp_path):
        merge_bars(tmp_path, item, iex)
        with pytest.raises(ValueError, match="Cannot mix Alpaca feeds"):
            merge_bars(tmp_path, item, sip)
    assert_frame_equal(load_bars(tmp_path, item), iex)


def test_invalid_tolerance_and_ambiguous_session_mode_rejected(bars, instrument):
    with pytest.raises(ValueError, match="Invalid reconciliation"):
        reconcile(bars, bars, relative_tolerance=float("nan"))
    with pytest.raises(ValueError, match="Configure session rules"):
        resample(bars, instrument.model_copy(update={"session": "all"}), "5m")

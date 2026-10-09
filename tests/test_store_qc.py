from datetime import UTC, datetime

import duckdb
import polars as pl
import pytest
from conftest import bars_at

from xasset.qc.checks import check_bars
from xasset.store.catalog import rebuild_catalog
from xasset.store.writer import (
    archive_raw,
    atomic_path,
    load_bars,
    merge_bars,
    writer_lock,
)


def test_store_partitions_utc_months_and_retains_revisions(tmp_path, instrument):
    bars = bars_at(
        [datetime(2026, 9, 30, 23, 59, tzinfo=UTC), datetime(2026, 10, 1, 0, 0, tzinfo=UTC)]
    )
    revised = bars.tail(1).with_columns(pl.lit(102.5).alias("close"))
    with writer_lock(tmp_path):
        assert merge_bars(tmp_path, instrument, bars) == 2
        assert merge_bars(tmp_path, instrument, bars) == 0
        assert merge_bars(tmp_path, instrument, revised) == 0
        coverage = rebuild_catalog(tmp_path)
    assert len(list((tmp_path / "bars/equity/TEST").glob("*.parquet"))) == 2
    assert load_bars(tmp_path, instrument)["close"].to_list() == [101.0, 102.5]
    assert coverage[0]["rows"] == 2
    with duckdb.connect(str(tmp_path / "catalog.duckdb"), read_only=True) as connection:
        assert connection.execute("SELECT rows FROM coverage").fetchone() == (2,)


def test_atomic_failure_preserves_original(tmp_path):
    target = tmp_path / "original"
    target.write_text("keep")
    with pytest.raises(RuntimeError), atomic_path(target) as temporary:
        temporary.write_text("partial")
        raise RuntimeError("disk interruption")
    assert target.read_text() == "keep"
    assert list(tmp_path.glob(".pending-*")) == []


def test_concurrent_writer_rejected(tmp_path):
    with writer_lock(tmp_path), pytest.raises(RuntimeError, match="Another recorder"):
        with writer_lock(tmp_path):
            pytest.fail("lock acquired twice")


def test_archive_checks_content_hash(tmp_path, instrument):
    with writer_lock(tmp_path):
        path = archive_raw(tmp_path, instrument, b'{"test":1}')
        assert archive_raw(tmp_path, instrument, b'{"test":1}') == path
        assert archive_raw(tmp_path, instrument, b'{"test":2}') != path
        path.write_text("corrupted")
        with pytest.raises(ValueError, match="checksum mismatch"):
            archive_raw(tmp_path, instrument, b'{"test":1}')


@pytest.mark.parametrize(
    "column,value,error",
    [
        ("high", 1.0, "invalid OHLC"),
        ("close", float("nan"), "invalid OHLC"),
        ("volume", -1, "negative volume"),
        ("close", None, "null required fields"),
    ],
)
def test_quality_rejects_corrupt_bars(bars, column, value, error):
    corrupt = bars.with_columns(pl.lit(value, dtype=bars.schema[column]).alias(column))
    assert error in check_bars(corrupt).errors


def test_duplicates_rejected_and_cannot_modify_store(tmp_path, instrument, bars):
    duplicate = pl.concat([bars, bars.head(1)])
    assert "duplicate symbol/timestamp" in check_bars(duplicate).errors
    with writer_lock(tmp_path), pytest.raises(ValueError, match="Quality gate"):
        merge_bars(tmp_path, instrument, duplicate)
    assert not list(tmp_path.rglob("*.parquet"))


def test_zero_volume_warning_and_negative_futures_prices(bars):
    zero = bars.with_columns(pl.lit(0, dtype=pl.Float64).alias("volume"))
    assert check_bars(zero).ok
    assert "ineligible for trade fills" in check_bars(zero).warnings[0]
    negative = bars.with_columns(
        (pl.col(name) - 1000).alias(name) for name in ("open", "high", "low", "close")
    )
    assert check_bars(negative).ok

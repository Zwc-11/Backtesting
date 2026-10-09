"""Single-writer locks and atomic file replacement on local filesystems.

POSIX uses ``flock``; Windows uses ``msvcrt.locking`` on the first byte of the lock
file. Both are non-blocking exclusive locks released when the context exits.
"""

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import polars as pl

if sys.platform == "win32":  # pragma: no cover - exercised on Windows hosts
    import msvcrt
else:
    import fcntl

from xasset.config import Instrument, Source
from xasset.qc.checks import check_bars
from xasset.store.schema import empty_bars, read_partition


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".writer.lock").open("a+") as handle:
        try:
            if sys.platform == "win32":  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError, OSError) as exc:
            raise RuntimeError("Another recorder owns this data directory") from exc
        try:
            yield
        finally:
            if sys.platform == "win32":  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def atomic_path(target: Path) -> Iterator[Path]:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if sys.platform != "win32":  # Windows cannot open directories for fsync.
            directory_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(target: Path, value: dict[str, Any]) -> None:
    with atomic_path(target) as temporary:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def archive_raw(
    root: Path,
    instrument: Instrument,
    payload: bytes,
    source: Source = "yahoo",
    suffix: str = "json",
) -> Path:
    if suffix not in {"json", "zip", "bi5", "CHECKSUM"}:
        raise ValueError("Unsupported raw archive suffix")
    digest = hashlib.sha256(payload).hexdigest()
    target = root / "raw" / source / instrument.id / f"{digest}.{suffix}"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Raw archive checksum mismatch: {target}")
        return target
    with atomic_path(target) as temporary:
        temporary.write_bytes(payload)
    return target


def instrument_path(root: Path, instrument: Instrument) -> Path:
    return root / "bars" / instrument.asset_class / instrument.id


def load_bars(root: Path, instrument: Instrument, source: Source | None = None) -> pl.DataFrame:
    base = root if source is None else root / "sources" / source
    files = sorted(instrument_path(base, instrument).glob("*.parquet"))
    if not files and source is not None:
        # Backward-compatible access to pre-Phase-1 canonical data.
        return load_bars(root, instrument).filter(pl.col("source") == source)
    return (
        pl.concat([read_partition(str(path)) for path in files]).sort("ts_end")
        if files
        else empty_bars()
    )


def merge_bars(root: Path, instrument: Instrument, incoming: pl.DataFrame) -> int:
    """Caller holds writer_lock. Latest settled provider revision wins per bar.

    Each monthly file is atomic. A multi-month run is recoverable by rerunning,
    but is not a transaction across all months. Raw responses retain revisions.
    Returns the increase in stored rows, not the number of revised rows.
    """
    report = check_bars(incoming)
    if not report.ok:
        raise ValueError(f"Quality gate rejected bars: {report.errors}")
    if incoming.filter(
        (pl.col("symbol") != instrument.id) | (pl.col("asset_class") != instrument.asset_class)
    ).height:
        raise ValueError("Incoming bars do not match the instrument")
    sources = incoming["source"].unique().to_list()
    if len(sources) != 1 or sources[0] not in instrument.sources:
        raise ValueError("Incoming source must be a single configured provider")
    source = sources[0]
    incoming = incoming.with_columns(
        pl.when(pl.col("volume") == 0)
        .then(pl.col("flags").list.set_union(["zero_volume"]))
        .when(pl.col("volume").is_null())
        .then(pl.col("flags").list.set_union(["volume_missing"]))
        .otherwise(pl.col("flags"))
        .alias("flags")
    )
    added = 0
    for key, group in (
        incoming.with_columns(pl.col("ts_end").dt.strftime("%Y-%m").alias("month"))
        .partition_by("month", as_dict=True)
        .items()
    ):
        month = str(key[0])
        target = instrument_path(root, instrument) / f"{month}.parquet"
        existing = read_partition(str(target)) if target.exists() else empty_bars()
        if not existing.is_empty() and not check_bars(existing).ok:
            raise ValueError(f"Existing partition failed quality checks: {target}")
        # Before replacing a legacy canonical partition, preserve every source
        # represented there. Otherwise a higher-priority import could erase the
        # only normalized copy of the older provider's overlapping observations.
        for old_source in existing["source"].unique().to_list():
            old_target = (
                instrument_path(root / "sources" / old_source, instrument) / f"{month}.parquet"
            )
            if not old_target.exists():
                with atomic_path(old_target) as temporary:
                    existing.filter(pl.col("source") == old_source).write_parquet(
                        temporary, compression="zstd"
                    )
        # Preserve normalized observations separately before choosing a canonical source.
        source_target = instrument_path(root / "sources" / source, instrument) / f"{month}.parquet"
        source_existing = (
            read_partition(str(source_target))
            if source_target.exists()
            else existing.filter(pl.col("source") == source)
        )
        if not source_existing.is_empty() and not check_bars(source_existing).ok:
            raise ValueError(f"Source partition failed quality checks: {source_target}")
        if not source_existing.is_empty() and source == "alpaca":
            old_feeds = {
                flag
                for flags in source_existing["flags"].to_list()
                for flag in flags
                if flag.startswith("feed_")
            }
            new_feeds = {
                flag
                for flags in incoming["flags"].to_list()
                for flag in flags
                if flag.startswith("feed_")
            }
            if old_feeds != new_feeds:
                raise ValueError("Cannot mix Alpaca feeds in the same instrument store")
        source_merged = (
            pl.concat([source_existing, group.drop("month")])
            .unique(subset=["symbol", "ts_end"], keep="last")
            .sort("ts_end")
        )
        if not source_merged.equals(source_existing):
            with atomic_path(source_target) as temporary:
                source_merged.write_parquet(temporary, compression="zstd")
        candidates = []
        for rank, preferred in enumerate(instrument.sources):
            preferred_path = (
                instrument_path(root / "sources" / preferred, instrument) / f"{month}.parquet"
            )
            candidate = (
                read_partition(str(preferred_path))
                if preferred_path.exists()
                else existing.filter(pl.col("source") == preferred)
            )
            if not candidate.is_empty() and not check_bars(candidate).ok:
                raise ValueError(f"Source partition failed quality checks: {preferred_path}")
            candidates.append(candidate.with_columns(pl.lit(rank).alias("priority")))
        merged = (
            pl.concat(candidates)
            .sort("priority")
            .unique(subset=["symbol", "ts_end"], keep="first")
            .drop("priority")
            .sort("ts_end")
        )
        added += merged.height - existing.height
        if not merged.equals(existing):
            with atomic_path(target) as temporary:
                merged.write_parquet(temporary, compression="zstd")
    return added

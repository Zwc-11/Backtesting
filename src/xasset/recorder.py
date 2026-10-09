"""One finite, retryable recorder run; scheduling belongs to the host."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import polars as pl

from xasset.config import Instrument
from xasset.ingest.yahoo_recorder import fetch, normalize
from xasset.normalize.timebase import minute_floor, utc
from xasset.store.catalog import rebuild_catalog
from xasset.store.writer import archive_raw, merge_bars, write_json, writer_lock


def record(
    root: Path,
    instruments: list[Instrument],
    start: datetime,
    end: datetime,
    *,
    client: httpx.Client,
    as_of: datetime | None = None,
    finalization_delay: timedelta = timedelta(minutes=20),
) -> dict[str, Any]:
    start, end = minute_floor(start), minute_floor(end)
    as_of = utc(as_of or datetime.now(UTC))
    if finalization_delay < timedelta(0):
        raise ValueError("finalization delay cannot be negative")
    if not timedelta(0) < end - start <= timedelta(days=30):
        raise ValueError("Recorder window must cover 1 minute to 30 days")
    if start < minute_floor(as_of) - timedelta(days=30) or end > as_of:
        raise ValueError("Yahoo minute requests must be within the last 30 days")
    run_id = f"{as_of:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "run_id": run_id,
        "started_at": as_of.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "finalization_delay_seconds": finalization_delay.total_seconds(),
        "status": "running",
        "instruments": [],
    }
    report_path = root / "runs" / f"{run_id}.json"
    with writer_lock(root):
        write_json(report_path, manifest)
        for instrument in instruments:
            result: dict[str, Any] = {
                "instrument_config": instrument.model_dump(mode="json"),
                "symbol": instrument.id,
                "status": "ok",
                "added_rows": 0,
                "accepted_rows": 0,
                "null_rows": 0,
                "excluded_rows": 0,
                "raw_files": [],
            }
            try:
                if instrument.yahoo_symbol is None:
                    raise ValueError(f"{instrument.id} has no Yahoo mapping; use xasset ingest")
                cursor = start
                while cursor < end:
                    chunk_end = min(cursor + timedelta(days=7), end)
                    payload = fetch(client, instrument.yahoo_symbol, cursor, chunk_end)
                    raw = archive_raw(root, instrument, payload)
                    result["raw_files"].append(str(raw.relative_to(root)))
                    normalized = normalize(
                        payload, instrument, cursor, chunk_end, as_of, finalization_delay
                    )
                    result["null_rows"] += normalized.null_rows
                    result["excluded_rows"] += normalized.excluded_rows
                    if not normalized.bars.is_empty():
                        result["added_rows"] += merge_bars(root, instrument, normalized.bars)
                        result["accepted_rows"] += normalized.bars.height
                    cursor = chunk_end
                if result["accepted_rows"] == 0:
                    result.update(
                        status="empty", error="No usable completed bars in requested window"
                    )
            except (httpx.HTTPError, ValueError, OSError, pl.exceptions.PolarsError) as exc:
                # Preserve successful symbols and raw evidence; the CLI still exits nonzero.
                result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            manifest["instruments"].append(result)
            write_json(report_path, manifest)
        try:
            manifest["coverage"] = rebuild_catalog(root)
        except (duckdb.Error, OSError, ValueError) as exc:
            manifest["catalog_error"] = f"{type(exc).__name__}: {exc}"
        manifest["status"] = (
            "ok"
            if instruments
            and "catalog_error" not in manifest
            and all(r["status"] == "ok" for r in manifest["instruments"])
            else "failed"
        )
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        write_json(report_path, manifest)
        write_json(root / "runs" / "latest.json", manifest)
    return manifest

"""Audited historical ingestion using the same quality gate and store as recording."""

import lzma
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import polars as pl

from xasset.config import Instrument, Source
from xasset.ingest import alpaca, binance_archive, dukascopy
from xasset.normalize.timebase import utc
from xasset.store.catalog import rebuild_catalog
from xasset.store.writer import archive_raw, merge_bars, write_json, writer_lock

ADAPTERS = {
    "binance": binance_archive.batches,
    "dukascopy": dukascopy.batches,
    "alpaca": alpaca.batches,
}


def ingest(
    root: Path,
    instruments: list[Instrument],
    source: Source,
    start: datetime,
    end: datetime,
    *,
    client: httpx.Client,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    start, end, now = utc(start), utc(end), utc(as_of or datetime.now(UTC))
    if source not in ADAPTERS:
        raise ValueError("Use record for Yahoo; ingest supports binance, dukascopy, and alpaca")
    if not timedelta(0) < end - start <= timedelta(days=366) or end > now:
        raise ValueError("Request a historical interval greater than zero and at most 366 days")
    if any(value.second or value.microsecond for value in (start, end)):
        raise ValueError("Historical ranges must align to whole minutes")
    run_id = f"ingest-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    report: dict[str, Any] = {
        "schema_version": 2,
        "run_id": run_id,
        "source": source,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "started_at": now.isoformat(),
        "status": "running",
        "instruments": [],
    }
    path = root / "runs" / f"{run_id}.json"
    with writer_lock(root):
        write_json(path, report)
        for instrument in instruments:
            result: dict[str, Any] = {
                "instrument_config": instrument.model_dump(mode="json"),
                "symbol": instrument.id,
                "status": "ok",
                "accepted_rows": 0,
                "added_rows": 0,
                "batches": [],
            }
            try:
                if source not in instrument.sources:
                    raise ValueError(f"{source} not configured for {instrument.id}")
                for batch in ADAPTERS[source](client, instrument, start, end, now):
                    evidence = []
                    for artifact in batch.artifacts:
                        raw_path = archive_raw(
                            root, instrument, artifact.payload, source, artifact.suffix
                        )
                        evidence.append(
                            {"path": str(raw_path.relative_to(root)), "url": artifact.url}
                        )
                    if not batch.bars.is_empty():
                        result["added_rows"] += merge_bars(root, instrument, batch.bars)
                        result["accepted_rows"] += batch.bars.height
                    result["batches"].append(
                        {
                            "start": batch.start.isoformat(),
                            "end": batch.end.isoformat(),
                            "rows": batch.bars.height,
                            "artifacts": evidence,
                            **batch.details,
                        }
                    )
                    # Persist progress even if a later archive/hour fails.
                    write_json(path, {**report, "active_instrument": result})
                if result["accepted_rows"] == 0:
                    result.update(status="empty", error="No usable bars in requested interval")
            except (
                ValueError,
                KeyError,
                TypeError,
                OSError,
                httpx.HTTPError,
                pl.exceptions.PolarsError,
                lzma.LZMAError,
                zipfile.BadZipFile,
            ) as exc:
                result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            report["instruments"].append(result)
            write_json(path, report)
        try:
            report["coverage"] = rebuild_catalog(root)
        except (duckdb.Error, OSError, ValueError) as exc:
            report["catalog_error"] = str(exc)
        report["status"] = (
            "ok"
            if instruments
            and "catalog_error" not in report
            and all(item["status"] == "ok" for item in report["instruments"])
            else "failed"
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(path, report)
        write_json(root / "runs" / "latest-ingest.json", report)
    return report

"""Rebuildable DuckDB coverage catalog. Parquet is the source of truth."""

from pathlib import Path
from typing import Any

import duckdb

from xasset.store.writer import atomic_path


def rebuild_catalog(root: Path) -> list[dict[str, Any]]:
    files = sorted(str(path) for path in (root / "bars").glob("*/*/*.parquet"))
    # Caller holds the writer lock; publish only a complete catalog.
    with atomic_path(root / "catalog.duckdb") as temporary:
        temporary.unlink()  # DuckDB must create its own nonempty database header.
        with duckdb.connect(str(temporary)) as connection:
            connection.execute(
                "CREATE TABLE coverage (symbol VARCHAR, asset_class VARCHAR, source VARCHAR, "
                "rows BIGINT, first_bar TIMESTAMPTZ, last_bar TIMESTAMPTZ)"
            )
            if files:
                connection.execute(
                    "INSERT INTO coverage SELECT symbol, asset_class, source, count(*), "
                    "min(ts_end), max(ts_end) FROM read_parquet(?) GROUP BY ALL",
                    [files],
                )
            rows = connection.execute("SELECT * FROM coverage ORDER BY symbol").fetchall()
    names = ("symbol", "asset_class", "source", "rows", "first_bar", "last_bar")
    return [
        {
            name: value.isoformat() if hasattr(value, "isoformat") else value
            for name, value in zip(names, row, strict=True)
        }
        for row in rows
    ]

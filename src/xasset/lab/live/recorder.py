"""Durable storage of live bars (one Parquet file per source, symbol and UTC day).

Recorded bars carry trades, quotes and their real arrival times, so they can prime
midpoint calibrations after a restart and be replayed later as a quote dataset.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import polars as pl

from xasset.lab.bars import FlowBar, lab_bar_path, to_frame
from xasset.store.writer import atomic_path, writer_lock


class BarRecorder:
    def __init__(self, root: Path, venue_symbols: dict[str, str]):
        """``venue_symbols`` maps instrument id to the venue symbol used as the folder."""
        self.root = root
        self.venue_symbols = venue_symbols
        self.buffer: dict[tuple[str, str, str], list[FlowBar]] = defaultdict(list)
        self.written = 0
        self.error: str | None = None

    def add(self, bars: list[FlowBar]) -> None:
        for bar in bars:
            venue = self.venue_symbols.get(bar.symbol)
            if venue is None:
                continue
            day = bar.start.strftime("%Y-%m-%d")
            self.buffer[(bar.source, venue, day)].append(bar)

    def flush(self) -> None:
        if not self.buffer:
            return
        pending = dict(self.buffer)
        try:
            with writer_lock(self.root / "lab"):
                for (source, venue, day), bars in pending.items():
                    target = lab_bar_path(self.root, source, venue) / f"{day}.parquet"
                    frame = to_frame(bars)
                    if target.exists():
                        frame = pl.concat([pl.read_parquet(target), frame])
                    frame = frame.unique("ts_end", keep="last").sort("ts_end")
                    with atomic_path(target) as temporary:
                        frame.write_parquet(temporary, compression="zstd")
                    self.written += len(bars)
                    del self.buffer[(source, venue, day)]
            self.error = None
        except (RuntimeError, OSError) as exc:  # lock held elsewhere: retry next flush
            self.error = f"{type(exc).__name__}: {exc}"[:200]

"""Resample only complete, contiguous minute windows; reset at session breaks."""

from datetime import timedelta
from typing import Any

import polars as pl

from xasset.config import Instrument
from xasset.normalize.calendars import calendar
from xasset.qc.checks import check_bars
from xasset.store.schema import BAR_SCHEMA, QUOTE_COLUMNS


def resample(bars: pl.DataFrame, instrument: Instrument, frequency: str) -> pl.DataFrame:
    if frequency not in {"5m", "1h", "1d"}:
        raise ValueError("Supported frequencies: 5m, 1h, 1d")
    qc = check_bars(bars)
    if not qc.ok or bars.filter(pl.col("symbol") != instrument.id).height:
        raise ValueError("Resampling requires structurally valid bars for one instrument")
    if not (
        (instrument.session == "regular" and instrument.calendar)
        or (instrument.session == "all" and instrument.asset_class == "crypto")
    ):
        raise ValueError("Configure session rules before resampling this instrument")
    window = {"5m": 5, "1h": 60, "1d": 1440}[frequency]
    lookup = {row["ts_end"]: row for row in bars.iter_rows(named=True)}
    first, last = min(lookup), max(lookup)
    groups: list[list[Any]] = []
    if instrument.session == "regular" and instrument.calendar:
        cal = calendar(instrument.calendar)
        for session in cal.sessions_in_range(first.date(), last.date()):
            times = [
                time.to_pydatetime() + timedelta(minutes=1) for time in cal.session_minutes(session)
            ]
            if frequency == "1d":
                groups.append(times)
            else:
                segments: list[list[Any]] = []
                for stamp in times:
                    if not segments or stamp - segments[-1][-1] != timedelta(minutes=1):
                        segments.append([])
                    segments[-1].append(stamp)
                for segment in segments:
                    # Final short session buckets are valid if all scheduled bars exist.
                    groups.extend(
                        segment[index : index + window] for index in range(0, len(segment), window)
                    )
    else:
        midnight = first.replace(hour=0, minute=0, second=0, microsecond=0)
        # A crypto bar ending at midnight belongs to the previous UTC day.
        if first == midnight:
            midnight -= timedelta(days=1)
        cursor = midnight
        while cursor < last:
            groups.append([cursor + timedelta(minutes=i) for i in range(1, window + 1)])
            cursor += timedelta(minutes=window)
    output = []
    for times in groups:
        if not times or times[-1] > last or not all(stamp in lookup for stamp in times):
            continue
        rows = [lookup[stamp] for stamp in times]
        sources = {row["source"] for row in rows}
        if len(sources) != 1:
            continue  # never hide a provider seam inside one aggregate
        result = dict(rows[0])
        result.update(
            ts_end=times[-1],
            high=max(row["high"] for row in rows),
            low=min(row["low"] for row in rows),
            close=rows[-1]["close"],
            volume=sum(row["volume"] for row in rows)
            if all(row["volume"] is not None for row in rows)
            else None,
            flags=sorted(
                {flag for row in rows for flag in row["flags"]} | {f"resampled_{frequency}"}
            ),
        )
        for side in ("bid", "ask"):
            names = [name for name in QUOTE_COLUMNS if name.startswith(side + "_")]
            if all(row[name] is not None for row in rows for name in names):
                result.update(
                    {
                        f"{side}_open": rows[0][f"{side}_open"],
                        f"{side}_close": rows[-1][f"{side}_close"],
                        f"{side}_high": max(row[f"{side}_high"] for row in rows),
                        f"{side}_low": min(row[f"{side}_low"] for row in rows),
                    }
                )
            else:
                result.update(dict.fromkeys(names))
        output.append(result)
    return pl.DataFrame(output, schema=BAR_SCHEMA).sort("ts_end")

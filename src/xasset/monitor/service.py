"""Finite capture cycles and an interruptible daemon with durable evidence."""

import json
import signal
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from xasset.monitor.alerts import deliver, enqueue, recent
from xasset.monitor.config import MonitorConfig
from xasset.monitor.detectors import detect
from xasset.monitor.feeds import fetch, market_window
from xasset.normalize.calendars import expected_bar_ends
from xasset.normalize.timebase import utc
from xasset.qc.checks import check_bars
from xasset.research.experiment import digest
from xasset.store.schema import empty_bars
from xasset.store.writer import archive_raw, merge_bars, write_json, writer_lock


def cycle(
    root: Path, config: MonitorConfig, client: httpx.Client, now: datetime | None = None
) -> dict[str, Any]:
    now = utc(now or datetime.now(UTC))
    live = root / "live"
    cutoff = (now - timedelta(seconds=config.settlement_seconds)).replace(second=0, microsecond=0)
    health, frames, evidence = [], [], []
    for item in config.instruments:
        try:
            start, end, is_open = market_window(item, cutoff, config.history_minutes)
            batch = fetch(client, item, start, end)
            quality = check_bars(batch.bars)
            if not quality.ok:
                raise ValueError("Feed bars failed structural quality checks")
            with writer_lock(live):
                artifacts = [
                    str(
                        archive_raw(live, item, a.payload, item.sources[0], a.suffix).relative_to(
                            root
                        )
                    )
                    for a in batch.artifacts
                ]
                merge_bars(live, item, batch.bars)
            latest = batch.bars["ts_end"].max()
            if not isinstance(latest, datetime):
                raise ValueError("Feed did not return a completed timestamp")
            age = (now - latest).total_seconds()
            expected = (
                expected_bar_ends(item.calendar, start, end)
                if item.session == "regular" and item.calendar
                else {
                    start + timedelta(minutes=i)
                    for i in range(1, int((end - start).total_seconds() // 60) + 1)
                }
            )
            missing = expected - set(batch.bars["ts_end"].to_list())
            state = (
                "missing"
                if missing
                else "market_closed"
                if not is_open
                else "stale"
                if age > config.stale_seconds
                else "healthy"
            )
            health.append(
                {
                    "symbol": item.id,
                    "source": item.sources[0],
                    "status": state,
                    "latest_at": latest.isoformat(),
                    "age_seconds": age,
                    "rows": batch.bars.height,
                    "expected_minutes": len(expected),
                    "missing_minutes": len(missing),
                    "first_missing": min(missing).isoformat() if missing else None,
                    "quality": quality.to_dict(),
                }
            )
            evidence.append(
                {
                    "symbol": item.id,
                    "requested_start": start.isoformat(),
                    "requested_end": end.isoformat(),
                    "received_at": datetime.now(UTC).isoformat(),
                    "artifacts": artifacts,
                }
            )
            frames.append(batch.bars)
        except (
            ValueError,
            OSError,
            httpx.HTTPError,
            KeyError,
            TypeError,
            pl.exceptions.PolarsError,
        ) as exc:
            health.append(
                {
                    "symbol": item.id,
                    "source": item.sources[0],
                    "status": "failed",
                    "error": type(exc).__name__,
                    "rows": 0,
                }
            )
    bars = pl.concat(frames) if frames else empty_bars()
    events, latest = detect(bars, config, now, health)
    queued = enqueue(root, events, config.cooldown_seconds)
    delivery = deliver(root, config, client)
    status = {
        "as_of": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "config_hash": digest(config.model_dump(mode="json")),
        "health": health,
        "latest": latest,
        "new_alerts": queued,
        "alerts": recent(root),
        "history": {
            item.id: [
                [row["ts_end"].isoformat(), row["close"]]
                for row in bars.filter(pl.col("symbol") == item.id).tail(180).iter_rows(named=True)
            ]
            for item in config.instruments
        },
        "delivery": delivery,
        "evidence": evidence,
        "healthy_feeds": len(
            {h["source"] for h in health if h["status"] in {"healthy", "market_closed"}}
        ),
        "lead_fired": {"enabled": False, "reason": "No strategy has passed all acceptance gates"},
        "orders_sent": 0,
    }
    with writer_lock(root):
        write_json(root / "monitor" / "latest.json", status)
        write_json(root / "monitor" / "cycles" / f"{now:%Y%m%dT%H%M%S%fZ}.json", status)
    return status


def serve(root: Path, config: MonitorConfig, once: bool = False) -> None:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with writer_lock(root / "monitor" / "daemon"):
        with httpx.Client(
            timeout=30, headers={"User-Agent": "xasset/0.1 observation-monitor"}
        ) as client:
            while not stop.is_set():
                try:
                    result = cycle(root, config, client)
                    print(
                        json.dumps(
                            {
                                "as_of": result["as_of"],
                                "healthy_feeds": result["healthy_feeds"],
                                "new_alerts": result["new_alerts"],
                            }
                        ),
                        flush=True,
                    )
                except (OSError, RuntimeError) as exc:
                    print(json.dumps({"status": "failed", "error": type(exc).__name__}), flush=True)
                    if once:
                        raise SystemExit(1) from exc
                    stop.wait(config.interval_seconds)
                    continue
                if once:
                    if any(h["status"] in {"failed", "stale", "missing"} for h in result["health"]):
                        raise SystemExit(1)
                    return
                stop.wait(config.interval_seconds)

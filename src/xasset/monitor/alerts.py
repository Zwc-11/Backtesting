"""Persistent deduplication, delivery receipts and explicit external-send enablement."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx

from xasset.monitor.config import MonitorConfig
from xasset.research.experiment import canonical_json
from xasset.store.writer import writer_lock


def enqueue(root: Path, events: list[dict[str, Any]], cooldown: int) -> int:
    queued = 0
    with writer_lock(root):
        with duckdb.connect(str(root / "monitor.duckdb")) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS alerts (id VARCHAR PRIMARY KEY, kind VARCHAR, "
                "symbol VARCHAR, event_at TIMESTAMPTZ, payload VARCHAR, status VARCHAR, "
                "attempts INTEGER DEFAULT 0, receipt VARCHAR)"
            )
            for event in sorted(events, key=lambda e: e["at"]):
                stamp = datetime.fromisoformat(event["at"])
                if db.execute("SELECT 1 FROM alerts WHERE id=?", [event["id"]]).fetchone():
                    continue
                recent = db.execute(
                    "SELECT 1 FROM alerts WHERE kind=? AND symbol=? AND "
                    "event_at>? AND event_at<=? AND status!='suppressed' LIMIT 1",
                    [event["kind"], event["symbol"], stamp - timedelta(seconds=cooldown), stamp],
                ).fetchone()
                state = "suppressed" if recent else "queued"
                db.execute(
                    "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                    [
                        event["id"],
                        event["kind"],
                        event["symbol"],
                        stamp,
                        canonical_json(event),
                        state,
                    ],
                )
                queued += state == "queued"
    return queued


def recent(root: Path, limit: int = 100) -> list[dict[str, Any]]:
    path = root / "monitor.duckdb"
    if not path.exists():
        return []
    with duckdb.connect(str(path), read_only=True) as db:
        rows = db.execute(
            "SELECT payload, status, attempts FROM alerts ORDER BY event_at DESC LIMIT ?", [limit]
        ).fetchall()
    return [
        {**json.loads(payload), "status": status, "attempts": attempts}
        for payload, status, attempts in rows
    ]


def deliver(root: Path, config: MonitorConfig, client: httpx.Client) -> dict[str, Any]:
    if not config.delivery_enabled or config.alert_mode == "log":
        return {"status": "log_only", "sent": 0}
    if config.alert_mode == "telegram":
        token, recipient = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not token or not recipient:
            return {"status": "missing_channel_credentials", "sent": 0}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
    else:
        url = os.getenv("DISCORD_WEBHOOK_URL", "")
        recipient = None
        if not url.startswith("https://discord.com/api/webhooks/"):
            return {"status": "missing_channel_credentials", "sent": 0}
    sent = 0
    with writer_lock(root):
        with duckdb.connect(str(root / "monitor.duckdb")) as db:
            rows = db.execute(
                "SELECT id, payload, event_at FROM alerts "
                "WHERE status='queued' ORDER BY event_at LIMIT 20"
            ).fetchall()
            for identity, payload, at in rows:
                if datetime.now(UTC) - at > timedelta(minutes=15):
                    db.execute("UPDATE alerts SET status='expired' WHERE id=?", [identity])
                    continue
                message = json.loads(payload)["message"]
                # Persist the dispatch claim before sending; an interrupted delivery
                # is uncertain and is not retried automatically (avoids duplicates).
                db.execute(
                    "UPDATE alerts SET status='dispatching', attempts=attempts+1 WHERE id=?",
                    [identity],
                )
                try:
                    body = (
                        {"chat_id": recipient, "text": message}
                        if recipient
                        else {"content": message}
                    )
                    response = client.post(url, json=body)
                    response.raise_for_status()
                    if recipient and not response.json().get("ok"):
                        raise ValueError("Telegram rejected the message")
                    status, receipt = "sent", f"HTTP {response.status_code}"
                    sent += 1
                except (httpx.HTTPError, ValueError) as exc:
                    status, receipt = "delivery_uncertain", type(exc).__name__
                db.execute(
                    "UPDATE alerts SET status=?, receipt=? WHERE id=?", [status, receipt, identity]
                )
    return {"status": "processed", "sent": sent}

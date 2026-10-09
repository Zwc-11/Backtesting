"""Read-only dashboard endpoints for the strategy lab and the live paper desk.

Everything is read from files the lab writes under ``data/lab``; nothing here can
start a run, open a holdout or send an order.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SCENARIOS = ("base", "costs_2x", "delay_1_bar")
STALE_SECONDS = 30


def safe(name: str) -> str:
    if not NAME.fullmatch(name) or ".." in name:
        raise HTTPException(404)
    return name


def read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tail_lines(path: Path, count: int, block: int = 65_536) -> list[str]:
    """The last ``count`` complete lines of a text file without reading all of it."""
    if not path.exists() or count <= 0:
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= count:
            step = min(block, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    if position > 0:
        lines = lines[1:]  # first line may be partial
    return [line for line in lines if line.strip()][-count:]


def records(path: Path, count: int) -> list[dict[str, Any]]:
    output = []
    for line in tail_lines(path, count):
        try:
            output.append(json.loads(line))
        except ValueError:
            continue
    return output


def all_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    output = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                try:
                    output.append(json.loads(line))
                except ValueError:
                    continue
    return output


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    scenarios = run.get("scenarios") or {}
    base = scenarios.get("base") or {}
    return {
        "id": run.get("id"),
        "book": run.get("book"),
        "kind": run.get("kind", "replay"),
        "folds": run.get("folds"),
        "phase": run.get("phase"),
        "status": run.get("status"),
        "error": run.get("error"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "period": run.get("period"),
        "variant": run.get("variant"),
        "amended_code": run.get("amended_code"),
        "universe": run.get("universe"),
        "strategies": {
            sid: data.get("summary", {}) for sid, data in (base.get("strategies") or {}).items()
        },
        "portfolio": {
            "start_nav": (base.get("portfolio") or {}).get("start_nav"),
            "end_nav": (base.get("portfolio") or {}).get("end_nav"),
        },
    }


def scenario_detail(run: dict[str, Any], name: str) -> dict[str, Any]:
    scenario = (run.get("scenarios") or {}).get(name)
    if scenario is None:
        raise HTTPException(404, "Scenario not found")
    portfolio = dict(scenario.get("portfolio") or {})
    trades = scenario.get("trades") or []
    return {
        "run": run_summary(run),
        "scenario": name,
        "settings": scenario.get("settings"),
        "minutes": scenario.get("minutes"),
        "bars": scenario.get("bars"),
        "strategies": scenario.get("strategies"),
        "multiplicity": scenario.get("multiplicity"),
        "portfolio": portfolio,
        "trade_count": len(trades),
        "requirements": run.get("strategies"),
    }


def downsample(points: list[list[Any]], limit: int) -> list[list[Any]]:
    if len(points) <= limit:
        return points
    stride = len(points) / limit
    chosen = [points[int(i * stride)] for i in range(limit - 1)]
    return [*chosen, points[-1]]


def mount(api: FastAPI, root: Path) -> None:
    lab = root / "lab"

    @api.get("/api/lab/catalog")
    def catalog() -> list[dict[str, Any]]:
        from xasset.lab.catalog import catalog as build

        return build()

    @api.get("/api/lab/runs")
    def runs() -> dict[str, Any]:
        summaries = []
        for path in sorted((lab / "runs").glob("*.json")):
            data = read(path)
            if isinstance(data, dict):
                summaries.append(run_summary(data))
        summaries.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        registry = []
        for path in sorted((lab / "registry").glob("*.json")):
            data = read(path)
            if isinstance(data, dict):
                registry.append(data)
        return {"runs": summaries, "registry": registry}

    @api.get("/api/lab/runs/{run_id}")
    def run(run_id: str, scenario: str = "base") -> dict[str, Any]:
        data = read(lab / "runs" / f"{safe(run_id)}.json")
        if not isinstance(data, dict):
            raise HTTPException(404, "Run not found")
        if scenario not in SCENARIOS:
            raise HTTPException(404, "Scenario not found")
        detail = scenario_detail(data, scenario)
        nav = detail["portfolio"].get("nav_daily") or []
        detail["portfolio"]["nav_daily"] = downsample(nav, 400)
        return detail

    @api.get("/api/lab/runs/{run_id}/trades")
    def run_trades(
        run_id: str, scenario: str = "base", strategy: str | None = None, limit: int = 300
    ) -> dict[str, Any]:
        data = read(lab / "runs" / f"{safe(run_id)}.json")
        if not isinstance(data, dict) or scenario not in SCENARIOS:
            raise HTTPException(404, "Run not found")
        trades = ((data.get("scenarios") or {}).get(scenario) or {}).get("trades") or []
        if strategy:
            trades = [t for t in trades if t.get("strategy") == strategy]
        trades = sorted(trades, key=lambda t: str(t.get("entry_time") or ""))
        limit = max(1, min(limit, 2000))
        return {"total": len(trades), "trades": trades[-limit:][::-1]}

    def desks() -> list[dict[str, Any]]:
        output = []
        now = datetime.now(UTC)
        for path in sorted((lab / "paper").glob("*/desk.json")):
            data = read(path)
            if not isinstance(data, dict):
                continue
            updated = data.get("updated_at")
            age = None
            if updated:
                age = (now - datetime.fromisoformat(updated)).total_seconds()
            data["age_seconds"] = age
            data["running"] = bool(data.get("running")) and age is not None and age < STALE_SECONDS
            output.append(data)
        return output

    @api.get("/api/paper")
    def paper() -> dict[str, Any]:
        return {"desks": desks(), "orders_enabled": False}

    def book_dir(desk: str, book: str) -> Path:
        path = lab / "paper" / safe(desk) / safe(book)
        if not path.is_dir():
            raise HTTPException(404, "Book not found")
        return path

    @api.get("/api/paper/{desk}/{book}/trades")
    def paper_trades(desk: str, book: str, limit: int = 200) -> dict[str, Any]:
        rows = all_records(book_dir(desk, book) / "trades.jsonl")
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:  # Funding after an exit re-emits the trade: last record wins.
            latest[str(row.get("id"))] = row
        trades = sorted(latest.values(), key=lambda t: t.get("exit_time") or "")
        by_strategy: dict[str, dict[str, float]] = {}
        for trade in trades:
            item = by_strategy.setdefault(
                trade.get("strategy", "?"), {"trades": 0, "wins": 0, "net": 0.0, "fees": 0.0}
            )
            item["trades"] += 1
            item["wins"] += 1 if trade.get("net", 0) > 0 else 0
            item["net"] += float(trade.get("net", 0.0))
            item["fees"] += float(trade.get("fees", 0.0))
        limit = max(1, min(limit, 2000))
        return {"total": len(trades), "by_strategy": by_strategy, "trades": trades[-limit:][::-1]}

    @api.get("/api/paper/{desk}/{book}/nav")
    def paper_nav(desk: str, book: str, points: int = 500) -> dict[str, Any]:
        rows = all_records(book_dir(desk, book) / "nav.jsonl")
        series = [[row["at"], row["nav"]] for row in rows if "at" in row and "nav" in row]
        return {"points": downsample(series, max(10, min(points, 2000))), "total": len(series)}

    @api.get("/api/paper/{desk}/{book}/events")
    def paper_events(desk: str, book: str, limit: int = 200) -> dict[str, Any]:
        path = book_dir(desk, book)
        limit = max(1, min(limit, 2000))
        return {
            "events": records(path / "events.jsonl", limit)[::-1],
            "orders": records(path / "orders.jsonl", limit)[::-1],
        }

"""Read-only lab and paper-desk endpoints of the dashboard API."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from xasset.monitor.api import app
from xasset.store.writer import write_json


def run_file(root: Path) -> str:
    run_id = "book-discovery-20261009T000000-abcdef"
    trade = {
        "id": "T1",
        "strategy": "h01",
        "symbol": "BTC",
        "entry_time": "2026-01-02T00:00:00+00:00",
        "exit_time": "2026-01-02T00:10:00+00:00",
        "net": 5.0,
    }
    later = {**trade, "id": "T2", "entry_time": "2026-01-03T00:00:00+00:00", "strategy": "h02"}
    scenario = {
        "settings": {"basis": "trade"},
        "minutes": 10,
        "bars": 20,
        "strategies": {"h01": {"title": "x", "summary": {"trades": 1}, "events": {}}},
        "multiplicity": {"h01": {"t": 1.0, "p_adjusted": 0.5}},
        "portfolio": {
            "start_nav": 100.0,
            "end_nav": 105.0,
            "nav_daily": [[f"2026-01-{d:02d}", 100.0 + d] for d in range(1, 31)] * 20,
            "episodes14": {"episodes": 0},
        },
        "trades": [later, trade],
    }
    write_json(
        root / "lab" / "runs" / f"{run_id}.json",
        {
            "id": run_id,
            "book": "book",
            "phase": "discovery",
            "status": "completed",
            "started_at": "2026-10-09T00:00:00+00:00",
            "period": {"report_from": "2026-01-01T00:00:00+00:00"},
            "scenarios": {"base": scenario, "costs_2x": scenario, "delay_1_bar": scenario},
        },
    )
    return run_id


def test_lab_runs_and_catalog(tmp_path: Path) -> None:
    run_id = run_file(tmp_path)
    write_json(tmp_path / "lab" / "registry" / "book.json", {"registration": {"book": "book"}})
    with TestClient(app(tmp_path)) as client:
        catalog = client.get("/api/lab/catalog").json()
        assert {item["id"] for item in catalog} >= {"h01", "h09", "h09t", "n01", "n10"}
        listing = client.get("/api/lab/runs").json()
        assert listing["runs"][0]["id"] == run_id and listing["registry"][0]["registration"]
        detail = client.get(f"/api/lab/runs/{run_id}?scenario=costs_2x").json()
        assert detail["scenario"] == "costs_2x" and detail["trade_count"] == 2
        assert len(detail["portfolio"]["nav_daily"]) == 400  # downsampled for the chart
        assert detail["portfolio"]["nav_daily"][-1] == ["2026-01-30", 130.0]
        trades = client.get(f"/api/lab/runs/{run_id}/trades").json()
        assert [t["id"] for t in trades["trades"]] == ["T2", "T1"]  # newest entry first
        only = client.get(f"/api/lab/runs/{run_id}/trades?strategy=h01").json()
        assert only["total"] == 1
        assert client.get(f"/api/lab/runs/{run_id}?scenario=other").status_code == 404
        assert client.get("/api/lab/runs/..%2Fregistry%2Fbook").status_code == 404
        assert client.get("/api/lab/runs/missing").status_code == 404


def test_paper_endpoints_read_the_desk_files(tmp_path: Path) -> None:
    desk = tmp_path / "lab" / "paper" / "crypto-paper"
    book = desk / "crypto-paper-trade"
    book.mkdir(parents=True)
    now = datetime.now(UTC)
    write_json(
        desk / "desk.json",
        {"id": "crypto-paper", "updated_at": now.isoformat(), "running": True, "books": []},
    )
    trade = {"id": "T1", "strategy": "h01", "net": 2.0, "fees": 1.0, "exit_time": now.isoformat()}
    adjusted = {**trade, "net": 2.5}  # funding re-emits the trade after its exit
    loser = {**trade, "id": "T2", "net": -1.0}
    (book / "trades.jsonl").write_text(
        "\n".join(json.dumps(r) for r in (trade, loser, adjusted)) + "\n"
    )
    nav = [
        {"at": (now - timedelta(minutes=i)).isoformat(), "nav": 100.0 + i}
        for i in range(1000, 0, -1)
    ]
    (book / "nav.jsonl").write_text("\n".join(json.dumps(r) for r in nav) + "\n")
    events = [{"at": now.isoformat(), "event": "armed", "n": i} for i in range(50)]
    (book / "events.jsonl").write_text("\n".join(json.dumps(r) for r in events) + "\n")
    with TestClient(app(tmp_path)) as client:
        paper = client.get("/api/paper").json()
        assert paper["orders_enabled"] is False
        assert paper["desks"][0]["running"] is True
        trades = client.get("/api/paper/crypto-paper/crypto-paper-trade/trades").json()
        assert trades["total"] == 2
        assert trades["by_strategy"]["h01"] == {"trades": 2, "wins": 1, "net": 1.5, "fees": 2.0}
        series = client.get("/api/paper/crypto-paper/crypto-paper-trade/nav?points=100").json()
        assert series["total"] == 1000 and len(series["points"]) == 100
        assert series["points"][-1][1] == 101.0
        tail = client.get("/api/paper/crypto-paper/crypto-paper-trade/events?limit=5").json()
        assert [e["n"] for e in tail["events"]] == [49, 48, 47, 46, 45]
        assert client.get("/api/paper/crypto-paper/..%2F..%2Fruns/trades").status_code == 404
        assert client.get("/api/paper/crypto-paper/unknown/trades").status_code == 404


def test_stale_desk_is_not_reported_as_running(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    write_json(tmp_path / "lab" / "paper" / "d" / "desk.json", {"updated_at": old, "running": True})
    with TestClient(app(tmp_path)) as client:
        assert client.get("/api/paper").json()["desks"][0]["running"] is False
        fonts = client.get("/assets/fonts/barlow-latin-400-normal.woff2")
        assert fonts.status_code == 200 and fonts.headers["content-type"] == "font/woff2"
        assert client.get("/assets/fonts/OFL.txt").status_code == 404

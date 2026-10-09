"""A registered run end to end: trades file, timelines, diagnostics and trade charts."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yaml
from fastapi.testclient import TestClient

from xasset.lab import research
from xasset.lab.bars import FLOW_SCHEMA, lab_bar_path
from xasset.monitor.api import app

SYMBOLS = ["BTC", "ETH", "A1", "A2", "A3", "A4"]
STRATEGIES = ["h01", "h01m", "h03", "h03m", "h04", "h04m", "h08", "h08m"]
START = datetime(2026, 3, 2, tzinfo=UTC)
DAYS = 11


def write_bars(root: Path) -> None:
    rng = np.random.default_rng(21)
    prices = dict.fromkeys(SYMBOLS, 100.0)
    rows: dict[str, list[dict[str, object]]] = {s: [] for s in SYMBOLS}
    for minute in range(DAYS * 1440):
        end = START + timedelta(minutes=minute + 1)
        common = rng.normal(0, 0.0012)
        for symbol in SYMBOLS:
            beta = 1.0 if symbol == "BTC" else 1.3
            move = beta * common + rng.normal(0, 0.0015)
            o = prices[symbol]
            c = o * math.exp(move)
            hi = max(o, c) * math.exp(abs(rng.normal(0, 0.0007)))
            lo = min(o, c) * math.exp(-abs(rng.normal(0, 0.0007)))
            notional = float(rng.lognormal(8, 0.6))
            buy = notional * float(np.clip(0.5 + 40 * move + rng.normal(0, 0.1), 0.02, 0.98))
            rows[symbol].append(
                {
                    "symbol": symbol, "ts_end": end, "available_at": end, "open": o,
                    "high": hi, "low": lo, "close": c, "volume": notional / c,
                    "notional": notional, "trades": 10, "buy_notional": buy,
                    "sell_notional": notional - buy, "unclassified_notional": 0.0,
                    "source": "binance-um",
                }
            )  # fmt: skip
            prices[symbol] = c
    for symbol, records in rows.items():
        frame = pl.DataFrame(
            records, schema_overrides={k: FLOW_SCHEMA[k] for k in FLOW_SCHEMA.names()}
        )
        for name in FLOW_SCHEMA.names():
            if name not in frame.columns:
                frame = frame.with_columns(pl.lit(None, dtype=FLOW_SCHEMA[name]).alias(name))
        target = lab_bar_path(root, "binance-um", symbol) / "2026-03.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.select(FLOW_SCHEMA.names()).write_parquet(target)


def write_book(folder: Path) -> Path:
    instruments = [
        {
            "id": s, "kind": "perp", "currency": "USDT", "tick": 0.01, "lot": 0.001,
            "cluster": f"c-{s}", "cost_class": "test", "shortable": True,
            "history": "binance-um",
        }
        for s in SYMBOLS
    ]  # fmt: skip
    universe = {
        "id": "test-perps",
        "description": "Synthetic perpetuals for the run test",
        "benchmark": "BTC",
        "minimum_peers": 2,
        "pairs": [{"leader": "BTC", "laggards": ["ETH", "A1"]}],
        "selection_note": "Synthetic instruments for an end-to-end test.",
        "instruments": instruments,
    }
    book = {
        "id": "test-perp-book",
        "universe": "universe.yaml",
        "strategies": STRATEGIES,
        "costs": {
            "test": {
                "half_spread_bps": 1, "impact_bps": 1, "fee_bps": 5,
                "evidence": "synthetic test costs",
            }
        },
        "calibration_sessions": 2,
        "minimum_reference": 20,
    }  # fmt: skip
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "universe.yaml").write_text(yaml.safe_dump(universe))
    path = folder / "book.yaml"
    path.write_text(yaml.safe_dump(book))
    return path


@pytest.fixture(scope="module")
def finished(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, object]]:
    root = tmp_path_factory.mktemp("run") / "data"
    write_bars(root)
    book = write_book(root.parent / "config")
    research.register(root, book, START, START + timedelta(days=DAYS))
    output = research.run(root, book, "discovery", workers=1)
    return root, output


def test_run_writes_trades_timelines_and_diagnostics(
    finished: tuple[Path, dict[str, object]],
) -> None:
    root, output = finished
    assert output["status"] == "completed", output.get("traceback")
    trades = pl.read_parquet(root / "lab" / "runs" / str(output["trades_file"]))
    assert set(trades["scenario"].unique()) <= {"base", "costs_2x", "delay_1_bar"}
    base = trades.filter(pl.col("scenario") == "base")
    assert base.height > 0
    assert base["timeline"].is_not_null().all()
    assert trades.filter(pl.col("scenario") != "base")["timeline"].is_null().all()
    assert (base["direction"] == -1).any(), "mirrors or h04 should produce shorts"
    diagnostics = output["diagnostics"]
    assert isinstance(diagnostics, dict) and diagnostics["markouts"]
    scenarios = output["scenarios"]
    assert isinstance(scenarios, dict) and "trades" not in scenarios["base"]


def test_trade_chart_and_examples_endpoints(finished: tuple[Path, dict[str, object]]) -> None:
    root, output = finished
    run_id = str(output["id"])
    with TestClient(app(root)) as client:
        listing = client.get(f"/api/lab/runs/{run_id}/trades?limit=500").json()
        assert listing["total"] >= len(listing["trades"]) > 0
        assert "timeline" not in listing["trades"][0]
        detail = client.get(f"/api/lab/runs/{run_id}").json()
        assert detail["has_trade_file"] and detail["diagnostics"]["markouts"]
        mirror = next((t for t in listing["trades"] if t["strategy"].endswith("m")), None)
        for trade in [listing["trades"][0], *([mirror] if mirror else [])]:
            chart = client.get(f"/api/lab/runs/{run_id}/trades/{trade['id']}/chart").json()
            bars = chart["series"]["asset"]["bars"]
            assert bars and chart["trade"]["id"] == trade["id"]
            lows = min(r[3] for r in bars)
            highs = max(r[2] for r in bars)
            assert chart["timeline"][0]["event"] == "armed"
            assert any(e["event"] == "context" for e in chart["timeline"])
            for level in chart["levels"]:
                if level["series"] == "asset":  # mirror levels come back on the real scale
                    assert 0.8 * lows < level["value"] < 1.2 * highs
            assert chart["mirror"] is trade["strategy"].endswith("m")
        examples = client.get(f"/api/lab/runs/{run_id}/examples").json()["examples"]
        if examples:
            first = examples[0]
            path = f"/api/lab/runs/{run_id}/examples/{first['strategy']}/{first['index']}/chart"
            chart = client.get(path).json()
            assert chart["series"]["asset"]["bars"] and chart["reason"] == first["reason"]
        assert client.get(f"/api/lab/runs/{run_id}/trades/nope/chart").status_code == 404

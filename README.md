# xasset — Cross-Asset Backtesting & World Monitor

Data ingestion, quality controls, a **native Python backtesting engine**, a
registered relationship map and a live observation dashboard
from the supplied [project plan](docs/PROJECT_PLAN.txt). Backtests run on the
project's own engine; see [research design and commands](docs/RESEARCH.md) and the
[strategy lab and paper desk](docs/LAB.md). No strategy has been accepted.

## Saved data and checkpoint

The [October 9 checkpoint release](https://github.com/Zwc-11/Backtesting/releases/tag/v0.1.0-checkpoint.20261009)
links to the complete verified data backup, its checksum manifest, built packages,
verification results and dashboard screenshots in
[`artifacts/checkpoint-20261009`](artifacts/checkpoint-20261009).
The backup is split into 32 MiB parts to fit GitHub's per-file limit.

To restore the saved data into a fresh clone after `uv sync --locked`:

```bash
# Verify and reassemble the parts; run from the repository root.
(cd artifacts/checkpoint-20261009 && sha256sum --check SHA256SUMS)
cat artifacts/checkpoint-20261009/xasset-20261009-final.tar.gz.part-* \
  > /tmp/xasset-20261009-final.tar.gz
cp artifacts/checkpoint-20261009/xasset-20261009-final.tar.gz.manifest.json /tmp/
uv run --frozen python scripts/backup.py restore /tmp/xasset-20261009-final.tar.gz data
```

Restore requires that `data` does not already exist. The backup contains the
research registry, raw observations, reports, map, live captures and archived
research implementation. Credentials and dependency caches are excluded.

On Windows, the restore command needs only Python 3.12; it automatically joins
the archive parts and verifies every checksum. In PowerShell, after cloning:

```powershell
Set-Location D:\Backtesting
py -3.12 scripts\backup.py restore-checkpoint artifacts\checkpoint-20261009 data
```

## Strategy lab and paper desk

The supplied strategy handbook runs as ten state-machine strategies on one causal
runtime: replayed over Binance and SIP archives (`xasset lab`) and live on a paper
desk fed by public Binance spot and Hyperliquid quotes (`xasset-app paper`), with
simulated bid/ask fills and no order routing. The dashboard shows a live state
board of every strategy on every instrument, open positions, results, registered
runs and data health. See [docs/LAB.md](docs/LAB.md). Model strategies 1, 3, 4 and 8
from the experimental notebook run as registered walk-forward studies on a
survivorship-aware Binance universe; see [docs/NOTEBOOK.md](docs/NOTEBOOK.md).

```bash
uv run --frozen xasset-app paper --config config/lab/paper-crypto.yaml
uv run --frozen xasset-app serve --host 127.0.0.1 --port 8000
```

## Implemented

- Yahoo recorder, checksum-verified Binance spot/USD-M monthly archives,
  Dukascopy hourly ticks, and an Alpaca historical adapter with pagination.
- Immutable raw artifacts, UTC bar-end timestamps, fractional volume, bid/ask/mid
  quote bars, atomic Parquet storage, and a DuckDB coverage catalog.
- Separate normalized provider observations and explicit canonical source priority.
  Existing integer-volume recordings remain readable and upgrade on write.
- Structural QC, anomaly reports, independent-source price comparisons, aligned
  reads with missing/stale/closed states, and complete 5m/1h/1d resampling.
- As-of split adjustments and explicit unadjusted futures contract chains as
  Python APIs, with leakage tests. These need verified actions/contract inputs.
- Locked dependencies, Ruff, strict mypy, pytest, CI, and a finite nightly
  recorder/health/audit script for deployment on a persistent host.
- Native causal lead/lag signals, cash/lot accounting, next-open fills,
  conservative stops, rolling training-only selection and 1x/2x costs.
- Immutable preregistration, persistent trial accounting, a one-use holdout,
  input snapshots, daily statistics, concentration checks and readable reports.
- A 30-instrument relationship graph with global false-discovery control,
  following-window stability checks and a persistent DuckDB edge registry.
- Three live feeds, shared causal features, persistent alert deduplication and a
  private four-view dashboard. [Operations and deployment](docs/OPERATIONS.md).
- Seven preregistered designs, batch resume and local HTML/Markdown pages;
  session-open, regional and event signals. See [Phase 3](docs/PHASE3.md).

## Verified in the current environment

The initial network blocker is resolved. The store contains September 2026 for
BTC/USDT spot, ETH/USDT spot, and BTC/USDT perpetuals: **129,600 minute bars**,
with archive checksums verified and no missing minutes. Each instrument produces
8,640 five-minute, 720 hourly, and 30 daily bars. Dukascopy EUR/USD and gold each
supplied one verified hour: 60 midpoint/bid/ask bars per instrument.

Phase 3 added 89,280 checksum-verified July/August BTC bars. BTC now has 132,480
stored July–September minutes and complete discovery coverage. The original IEX
backfill retains 501,039 bars and its original blocked experiments.

A separately preregistered historical **SIP** revision acquired **576,013 bars
for 29 equities across 87 verified chunks**. All 29 exceed 95% coverage. The
bitcoin-to-equities experiment completed and was **rejected**; six experiments
still lack verified CFD inputs, with EIA also lacking a point-in-time calendar.
Dukascopy now returns an explicit bot block and directs exports to a paid
requester-pays service; requests were stopped. See the
[SIP verdict pages](data/reports/completion/sip-campaign/index.html) and
[audit](data/reports/completion/sip-audit.html).

The registered map evaluated **29,145 cells** across 30 instruments at 5m, 1h and
1d: **570 stable descriptive relationships**, 479 significant only in the first
window, and 28,096 candidates. **Zero tradable edges; every vault stays locked.**
The monitor captured complete bounded windows from Alpaca IEX, Kraken and
Hyperliquid. Chromium checks cover desktop/mobile layout, all four views,
map filters and evidence selection. Alerts remain log-only.

All five Yahoo pilot instruments recorded successfully. Shell London has five
complete sessions. Toyota and HSBC have gaps in the source responses; they stay
flagged as incomplete. Futures proxies have unknown session coverage.

**Phase 1 acceptance remains open:** Massive/HistData comparisons, a full
universe backfill, verified FX/CFD sessions,
and real dated futures contracts remain outstanding. The always-on host is not
deployed. See [milestones](docs/ROADMAP.md).

## Quick start

Use the existing checkout; cloud tasks are already isolated, so no additional
Git worktree is needed. Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
cd /workspace/Backtesting
uv sync --locked
make check
uv run --frozen xasset --help

# Offline synthetic engine smoke run; uses its own fresh data directory.
uv run --frozen xasset research demo > /tmp/xasset-demo.json

# Initial window covering five complete sessions.
uv run --frozen xasset record --days 10
uv run --frozen xasset health --symbols SHELL_UK --sessions 5

# Completed monthly archive, checked against published SHA-256.
uv run --frozen xasset ingest --source binance --symbols BTC_USDT \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z
uv run --frozen xasset build --universe config/history.yaml --symbols BTC_USDT --freq 5m
uv run --frozen xasset audit --universe config/history.yaml --symbols BTC_USDT
uv run --frozen xasset catalog
```

Start the dashboard in a separate terminal with
`uv run --frozen xasset-app serve --host 127.0.0.1 --port 8000`. See
[operations](docs/OPERATIONS.md) to start its capture process or deploy it.

`config/universe.yaml` is the five-instrument forward pilot. `config/history.yaml`
adds a seven-instrument historical pilot. `ingest` defaults to the historical
universe and filters by source unless symbols are given. Other commands default
to the forward universe; supply `--universe` explicitly.

`XASSET_DATA_DIR` selects the store (default `./data`). Public sources need no key.
Alpaca reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` from the process environment;
enter them securely in environment settings. `.env` files are not auto-loaded.
No paid provider or trading endpoint is called.

## Storage and data contract

```text
data/
  raw/{source}/{symbol}/{sha256}.{json,zip,CHECKSUM,bi5}
  sources/{source}/bars/{asset_class}/{symbol}/{YYYY-MM}.parquet
  bars/{asset_class}/{symbol}/{YYYY-MM}.parquet
  derived/{5m,1h,1d}/{asset_class}/{symbol}.parquet
  catalog.duckdb
  registry.duckdb                 # immutable research definitions and all attempts
  research/{run_id}/input.parquet # exact permitted input to that research run
  runs/{run_id}.json
  runs/latest.json                 # forward recorder
  runs/latest-ingest.json          # historical ingestion
  reports/                        # audit/reconciliation outputs
```

A minute bar covers `[ts_end - 1 minute, ts_end)`. Missing price and volume are not
forward filled. Volume is floating point to preserve crypto quantity. Dukascopy
stores bid/ask OHLC and true tick-mid OHLC with null traded volume.

The `sources` list sets priority per instrument. A lower-priority source fills
uncovered timestamps without overwriting preferred observations; both inputs
remain available for comparison. Return features break at source changes and
explicit roll boundaries. IEX and SIP cannot silently share one Alpaca store.

Monthly files are atomic; a multi-file run is recoverable, not transactional.
Reruns can revise settled bars while retaining raw evidence. These revisions are
not historical point-in-time snapshots. All observations remain single-source
until independently reviewed; capture and structural QC are not the full
research validation gate.

See the [runbook](docs/RUNBOOK.md), [source contracts](docs/DATA_SOURCES.md), and
[validation boundary](docs/VALIDATION.md) for limits and commands.

The native engine trades long-only cash equities/ETFs and spot crypto in one
ledger currency. Quote-only drivers may supply returns in their own currencies.
Derivative positions, shorts, FX conversion, portfolio
strategies, and live execution are pending. Cost calibration, independent data
evidence, an independent audit and Nautilus reconciliation remain acceptance
requirements. Alpaca credentials are verified in this workspace; offline tests need none.

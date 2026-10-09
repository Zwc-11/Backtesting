# Strategy lab and paper desk

The lab tests the supplied strategy documents under one causal runtime, in two
places:

- **Replay** (`xasset lab`): registered books replayed minute by minute over
  stored history, with a sealed holdout, cost and delay stress and multiplicity
  control.
- **Paper desk** (`xasset-app paper`): the same runtime, strategies, sizing and
  limits on live public market data, with fills simulated against the live bid and
  ask. Nothing is ever sent to an exchange or broker.

The dashboard (`xasset-app serve`) shows both: the desk's state board, positions and
results; every strategy's status; registered runs; relationships; data health.

## What runs where

| Strategy | Replay (archives) | Paper desk | Why |
|---|---|---|---|
| h01 Strength during market weakness | crypto perpetuals, crypto spot, US large caps | quote and trade-bar books | needs benchmark, 20+ peers, residual model |
| h02 Buying bursts with retained gains | crypto perpetuals, spot | both books | needs aggressor-labelled flow (US bars have none) |
| h03 Improving pullbacks after broad breakout | crypto perpetuals, spot | both books | flow and breadth |
| h04 Failed recovery into a high-volume area (short) | crypto perpetuals | both books (Hyperliquid perpetuals) | needs short access; equity borrow data is unavailable |
| h05 Residual breadth before index response | blocked | blocked | needs a complete point-in-time constituent set |
| h06 Resistance approached with less buying | crypto perpetuals, spot | both books | flow |
| h07 Rising trading centre inside narrowing range | crypto perpetuals, spot, US large caps | both books | trade VWAP (Alpaca bars carry it) |
| h08 Confirmed catch-up after common shock | crypto perpetuals, spot, US large caps | both books | 60 sessions for the lead-lag stability screen |
| h09 Selling bursts with shrinking damage | as h09t only | quote book | the primary version compares quoted spreads |
| h09t (trade-bar variant of 9, no spread rule) | crypto perpetuals, spot | trade-bar book | separately registered variant |
| h10 Thin-session break survives the handoff | crypto (NYSE-open handoff) | both books | equities need pre-market bars |
| h01m-h10m mirror variants | crypto perpetuals | — | the same rule on 1/P, opposite side (see below) |
| n01, n03, n08 (experimental notebook) | daily Binance study incl. delisted coins | — | model strategies; see [NOTEBOOK.md](NOTEBOOK.md) |
| n04 (experimental notebook) | minute-data study, 28 coins | — | censored recovery clocks; see [NOTEBOOK.md](NOTEBOOK.md) |
| n02, n05 | next | — | data in place, models not implemented |
| n07, n10 | not built | — | need trade-by-trade events (Binance aggTrade archives) |
| n06, n09 | blocked | — | need order-book depth, recorded forward |

`uv run --frozen xasset lab catalog` prints the same table with data needs and
blockers; the dashboard's Strategies page renders it with a stage-by-stage
explanation of every strategy and a real trade and skipped setup from the latest run.

## Books

| Book | Universe | Strategies | Period |
|---|---|---|---|
| `crypto-perp` | the 50 largest Binance USD-M crypto perpetuals each month (point in time) | handbook 1-4, 6-10 (h09t) and their mirrors, long and short | Jan 2024 - Sep 2026 |
| `us-large` | the 50 US stocks with the highest dollar volume each month, plus SPY, QQQ, IWM, SMH, sector ETFs | h01, h07, h08 (long) | Jan 2024 - Sep 2026 (download on your PC) |
| `crypto-archive` | 28 spot coins and 12 perpetuals chosen once (superseded) | handbook 1-4, 6-10 | Jan - Sep 2026 |
| `us-archive` | 11 thematic stocks and 18 ETFs (superseded) | h01, h08 | Jul - Sep 2026 |

### Point-in-time perpetual universe

`xasset lab perp-universe` lists every USD-M contract ever archived on
data.binance.vision (886 in October 2026, delisted ones included), downloads its
checksum-verified daily klines, and for each month keeps the 50 with the largest quote
notional over the previous complete month (a contract needs a bar on every day of that
month). From 28 January 2026 Binance also lists perpetuals on stocks, ETFs and
commodities (AAPL, NVDA, SPY, QQQ, crude oil, natural gas and many more); they trade
weekdays only (weekend notional 7-35% of weekdays, against 36% and above for crypto) and
are excluded by a reviewed list plus a point-in-time weekday-trading rule, together
with stablecoins, metals and composite indices. Membership churns: 329 contracts over
33 months. Arming and breadth use members only; a replay loads the bars of members of
the previous, current and next two months (exits across a month boundary, 20-session
calibrations, 60-session pair screens). Ticks and lots are inferred from archive prices
and volumes because the futures API is not reachable from the build environment.

### Mirror variants

The mirror of a strategy runs the unchanged rule on the inverted market: price 1/P (a
high becomes a low), buyer- and seller-initiated notional exchanged (a taker buying the
coin is a taker selling USDT), base volume replaced by notional^2/volume so the
volume-weighted price inverts exactly. Every quantile, robust scale, residual model and
breadth count of the mirrored market is therefore the exact mirror of the original, and
no short rule is written by hand. A candidate found on 1/P is converted back: the order
takes the opposite side, the stop is 1/K', "below" guards become "above" guards.
Mirrors of long rules are shorts on perpetuals; the mirror of h04 is a long. They are
new hypotheses, registered and reported separately (IDs ending in `m`), and they share
capital and the one-position-per-coin rule with the originals.

### What every run records

- **Trades** in `data/lab/runs/<run>/trades.parquet` (all scenarios), each base trade
  with its full setup timeline: arming with the frozen anchors, every state change,
  the wider market at the decision, the confirmation, the fill and the exit.
- **Skipped setups**: a seeded random sample of setups per strategy and expiry reason,
  and refused or cancelled orders, in `data/lab/runs/<run>/examples.json`.
- **Entry diagnostics**: the price move 1, 5, 15, 30 and 60 minutes after each signal
  (before costs, whatever the exit), with day-clustered standard errors, next to random
  entries on the same coin and time of day; and results split by the market regime at
  the decision (benchmark up or down since the prior close and over 4 hours, calm or
  volatile last hour).
- The dashboard draws any trade or skipped setup on its minute chart: the coin's
  candles, the market below, buyer and seller volume, the frozen levels, stop, target,
  entry and exit. Charts read the local archives, so download them on the computer
  that serves the dashboard.

## Architecture

```
            Binance archives            Binance spot + Hyperliquid websockets
          (klines, funding, daily)        (aggTrade, bookTicker, trades, bbo)
                   │                                   │
                   ▼                                   ▼
         lab/ingest.py, warmup.py            live/feeds.py → live/events.py
                   │                         live/aggregator.py (minute bars)
                   │                                   │  live/recorder.py
                   ▼                                   ▼
              FlowBar (one-minute bar: trade OHLC, flow, quote mids, spreads)
                                   │
                                   ▼
          lab/runtime.py  ── lab/market.py (sessions, calibrations, models)
                │         ── lab/strategies/h01…h10 (state machines)
                │         ── lab/portfolio.py (sizing, hard limits)
                ▼
     ┌──────────┴──────────┐
     │                     │
 lab/execution.py      live/broker.py
 BarBroker (replay)    QuoteBroker (paper: live bid/ask, latency, funding)
     │                     │
     ▼                     ▼
 lab/research.py       live/desk.py (books, warm-up, restarts, snapshots)
 lab/evaluate.py           │
     │                     ▼
     ▼               data/lab/paper/<desk>/…
 data/lab/runs/*.json         │
     └──────────┬─────────────┘
                ▼
     monitor/lab_api.py → web/ (dashboard)
```

### Files

```
src/xasset/lab/
  bars.py          FlowBar and its Parquet schema
  ingest.py        checksum-verified Binance kline and funding archives
  universe.py      instruments, universes, books, costs, limits, execution settings
  sessions.py      exchange calendars and UTC-day sessions
  calibration.py   prior-only, time-of-session calibrations (20 sessions, ±15 minutes)
  market.py        tapes, features, residual and lead-lag models
  strategy.py      state-machine base class (armed → confirmed → ordered → open → cooldown)
  strategies/      h01–h10 and h09t
  portfolio.py     sizing rule and hard exposure limits
  execution.py     orders, accounting, BarBroker
  runtime.py       the causal minute loop shared by replay and paper
  backtest.py      replay over stored bars
  evaluate.py      HAC t, drawdown, fourteen-day windows, block-bootstrap max-T
  research.py      registration, discovery runs, one-use holdout
  catalog.py       every supplied strategy, where it runs and why
  cli.py           `xasset lab …`
  live/
    events.py      trades, quotes, recent history
    clock.py       local clock corrected against exchange server time
    feeds.py       Binance spot and Hyperliquid websocket clients
    aggregator.py  live minute bars (kline conventions, quote mids, spread samples)
    broker.py      QuoteBroker: entries, stops, targets, time exits, funding
    recorder.py    recorded live bars (data/lab/bars/paper-<feed>/…)
    warmup.py      archive and recorded history used to prime calibrations
    desk.py        the desk process: books, warm-up, persistence, snapshots
config/lab/
  crypto-universe.yaml, crypto-book.yaml    replay of Binance archives
  us-universe.yaml, us-book.yaml            replay of stored SIP equity bars
  crypto-paper-universe.yaml                live universe (28 spot + 12 perpetuals)
  crypto-paper-quote.yaml                   paper book, primary (midpoint) versions
  crypto-paper-trade.yaml                   paper book, trade-bar variants
  paper-crypto.yaml                         the desk: universe, books, warm-up
src/xasset/monitor/lab_api.py               read-only dashboard endpoints
src/xasset/web/                             dashboard (index.html, app.js, style.css, fonts)
```

## Conventions implemented from the handbook

- Bars on a fixed UTC grid; bar `t` covers `(t − 60 s, t]` and is usable only after
  its arrival time. Decisions happen at arrival plus decision latency (250 ms).
- Entries execute at the first eligible quote at or after the decision plus transport
  latency (150 ms); never at the close used to recognise the signal.
- Calibrations use only the previous 20 complete sessions in a ±15-minute
  time-of-session neighbourhood, need at least 200 observations, and are frozen
  per session. Scales use 1.4826 × MAD with a one-tick floor; σ is frozen at arming.
- Flow needs at least 95% classified notional per event window; unknown flow is
  never treated as zero.
- Stop distance filter d_ref ∈ [0.25 σ10, 3 σ10]; target P·exp(±2d); 60-minute time
  exit unless a strategy overrides it; 15-minute cooldown; one position per asset
  under a frozen priority order (1, 2, 9, then the rest).
- Sizing: min(0.1% NAV risk, 10% NAV notional, 1% of expected next-minute notional);
  gross ≤ NAV, aggregate stop risk ≤ 1% NAV, cluster gross ≤ 25%, pending orders count.
- Replay on bars: adverse ordering when a stop and target share a bar, with the
  favourable alternative recorded; a wrong-side stop exits at the next open.
- Paper on quotes: stops and targets trigger on the executable side (bid for longs,
  ask for shorts) and exit at the next eligible quote after latency, so gaps are paid.
  Perpetual funding is applied when Hyperliquid publishes it (positive rate: longs pay).

## Replay commands

Crypto perpetuals (point in time, long and short):

```bash
uv run --frozen xasset lab perp-universe --start 2024-01-01T00:00Z --end 2026-09-01T00:00Z \
  --top 50 --out config/lab/crypto-perp-universe.yaml          # already in the repository
uv run --frozen xasset lab ingest-universe config/lab/crypto-perp-universe.yaml \
  --start 2024-01-01T00:00Z --end 2026-10-01T00:00Z            # about 3.5 GB
uv run --frozen xasset lab register config/lab/crypto-perp-book.yaml \
  --start 2024-01-01T00:00Z --end 2026-10-01T00:00Z
uv run --frozen xasset lab run config/lab/crypto-perp-book.yaml
```

Large US stocks (needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the environment or
in `.env`; only historical market-data requests are made):

```bash
uv run --frozen xasset lab us-universe --start 2024-01-01T00:00Z --end 2026-09-01T00:00Z \
  --top 50 --out config/lab/us-large-universe.yaml
uv run --frozen xasset lab ingest-universe config/lab/us-large-universe.yaml \
  --start 2024-01-01T00:00Z --end 2026-10-01T00:00Z
uv run --frozen xasset lab register config/lab/us-large-book.yaml \
  --start 2024-01-01T00:00Z --end 2026-10-01T00:00Z
uv run --frozen xasset lab run config/lab/us-large-book.yaml
```

Earlier books:

```bash
# Every archive the crypto universe needs (28 spot coins, 12 USD-M perpetuals, funding):
uv run --frozen xasset lab ingest-universe config/lab/crypto-universe.yaml \
  --start 2026-01-01T00:00Z --end 2026-10-01T00:00Z
# Or symbol by symbol:
uv run --frozen xasset lab ingest --market spot --symbols BTCUSDT ETHUSDT \
  --start 2026-01-01T00:00Z --end 2026-10-01T00:00Z
uv run --frozen xasset lab ingest --market um --symbols BTCUSDT --funding \
  --start 2026-01-01T00:00Z --end 2026-10-01T00:00Z
uv run --frozen xasset lab register config/lab/crypto-book.yaml \
  --start 2026-01-01T00:00Z --end 2026-10-01T00:00Z
uv run --frozen xasset lab run config/lab/crypto-book.yaml            # discovery
uv run --frozen xasset lab holdout-open crypto-archive --reason "…"   # once
uv run --frozen xasset lab run config/lab/crypto-book.yaml --phase holdout
```

A run replays three scenarios (base, doubled costs, one bar of extra delay) in
parallel processes, one per core up to three; `--workers 1` runs them one after
another. On two cores the nine-month crypto book takes about an hour.

Registration freezes the book file, the universe file and the replay code; any
change to either file requires a new book ID. The final 20% of the registered
interval is the holdout. Every run, including failures, is stored in
`data/lab/runs/` and listed on the dashboard.

## Paper desk

```bash
uv run --frozen xasset-app paper --config config/lab/paper-crypto.yaml
uv run --frozen xasset-app serve --host 127.0.0.1 --port 8000   # second terminal
```

On start the desk measures its clock against Binance server time, opens the feeds
and replays history so calibrations start from prior sessions:

- **Trade-bar book**: 62 days of Binance spot klines (monthly and daily archives,
  checksum-verified and cached under `data/lab/bars/`, plus the latest minutes from
  the public market-data API). It trades from the first live minute.
- **Quote book**: only bars this desk recorded, because klines have no quotes. It
  starts arming after 20 complete recorded UTC days (strategy 8 after 60). Keep the
  desk running; recorded bars accumulate in `data/lab/bars/paper-*/`.

Hyperliquid perpetuals have no public archive with aggressor flow, so in both books
they calibrate from recorded sessions.

Outputs in `data/lab/paper/crypto-paper/`: `desk.json` (live snapshot) and, per book,
`events.jsonl`, `orders.jsonl`, `fills.jsonl`, `trades.jsonl`, `nav.jsonl` and
`state.json`. After a restart, cash and open positions are restored from
`state.json`; orders that had not filled are recorded as cancelled. Minutes during
which a feed was disconnected are never emitted, and a stalled process skips the
unobserved minutes instead of inventing them.

Stop the desk with Ctrl+C; it saves its state and flushes recorded bars.

## Windows

```powershell
Set-Location D:\Backtesting
uv sync --locked
uv run --frozen pytest -q
uv run --frozen xasset-app paper --config config\lab\paper-crypto.yaml
# second PowerShell window
uv run --frozen xasset-app serve --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

File locks use `msvcrt` on Windows. The desk writes snapshots atomically and retries
briefly while the dashboard reads them. Keep the computer from sleeping while the
desk runs; a sleeping machine produces skipped minutes, which the desk records
rather than fills.

## Adding strategies

See [ADDING_STRATEGIES.md](ADDING_STRATEGIES.md) for both kinds (state machines and notebook
models): the class contract, registration, tests and books.

## Rule-by-rule audit

Every handbook strategy was checked against the handbook text and its equations
(October 2026): arming conditions, frozen anchors, state transitions, expiries, stops,
targets and time exits match. Where the text leaves room, the implementation takes
the causal reading and says so in the strategy's docstring: h02 counts every minute
after a burst as a pause observation until the next block proves to be a burst; h04
builds the volume profile from minute VWAPs; h07 calibrates block notional with rolling
ten-minute notional; h09 ends a sequence when a burst follows a recovery without a quiet
block. The common rules (stop distance within 0.25-3 sigma_10, target at two risk
units, 60-minute time exit, 15-minute cooldown, adverse same-bar ordering, next-quote
entry, wrong-side and gap handling) are enforced by the runtime and broker.

## Limitations

- Impact (1–3 bps per side) is an uncalibrated assumption on top of real spreads.
- The crypto universe was selected from coins still listed in October 2026
  (survivorship caveat documented in the universe file).
- Trade-bar variants are separate hypotheses from the primary quote versions.
- US equities are replayed only; a US paper desk needs SIP-quality quotes.
- Notebook strategies 1, 3, 4 and 8 run as registered walk-forward studies
  ([NOTEBOOK.md](NOTEBOOK.md)); 2 and 5 are next; 6, 7, 9 and 10 need data that no
  public archive provides. Notebook strategies are not yet wired to the paper desk.

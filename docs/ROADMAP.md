# Project acceptance status — October 9, 2026 UTC

The local engineering foundation now includes the relationship map, three-feed
monitor and dashboard. **The entire project is not yet accepted or deployed.**
No strategy is accepted, no vault is open, no orders or phone messages were sent.

## Phase 0 — recorder implemented; persistent operation open

All five Yahoo pilot instruments have actual capture evidence. Shell London has
five complete sessions. Toyota and HSBC have missing source minutes; ES/WTI are
front-month proxies with unverified coverage. Docker/systemd deployment templates
and verified backup/restore are available. An always-on host, scheduler deployment
and sustained daily health remain outstanding.

## Phase 1 — historical store implemented; wider data acceptance open

- Verified Binance monthly archives contain 129,600 September spot/perpetual bars,
  plus 89,280 July/August BTC bars. No missing minutes in these archives.
- Authenticated Alpaca IEX: 501,039 discovery bars, 29 symbols, 87 audited chunks;
  11 symbols pass the original 95% coverage requirement.
- Separately registered Alpaca SIP: 576,013 discovery bars, 29 symbols, 87 audited
  chunks; **all 29 pass 95% coverage**. IEX and SIP identities remain separate.
- Dukascopy decoding, source-separated storage, structural QC, calendar-aware
  resampling, gap/stale-aware reads and raw checksum evidence are implemented.
- Point-in-time split transformations and explicit unadjusted futures rolls have
  known-answer/leakage tests; they still need verified production inputs.

Remaining: choose/backfill the proposed broader universe; Massive/HistData
adapters and real independent-source comparisons; verified broker CFD metadata
and calendars; dated futures contracts and roll schedules; corporate-action
ledger and dividend/total-return handling. Dukascopy's current bot block directs
exports to a paid requester-pays service, so acquisition is stopped pending an
authorized source. Fixture tests cannot satisfy these evidence requirements.

## Phase 2 — native engine and controls implemented; independent acceptance open

Backtests run on the project's own engine; the unavailable prior benchmark is retired.
Native execution has exact fill, cost, cash/lot and causal-invariance fixtures.
Immutable experiment/grid/universe/cost registration, cumulative trial penalties,
last-20% vaults, purged/embargoed selection, doubled costs, daily statistics,
concentration, signal delay and durable failed-attempt records are implemented.

Remaining: independent data/action/coverage review, measured costs, statistics
review, independent leakage audit and a real Nautilus reconciliation. The current
engine supports long-only cash equities/ETFs and spot crypto in one currency;
shorts, derivative execution and simultaneous portfolios are still extensions.

## Phase 3 — seven current pages; one rejected, six awaiting inputs

All 316 original candidates and 316 separate SIP candidates remain registered.
The SIP bitcoin-equities discovery completed: positive net PnL at 1x/2x costs,
but failed statistical, concentration and delay criteria; verdict **rejected**.
Six families lack verified CFD inputs; EIA also lacks a point-in-time schedule.
No holdout was opened. The other deferred hypothesis slices remain explicitly
listed in configuration. See [PHASE3.md](PHASE3.md) for exact evidence and the
archived implementation required to resume old registrations.

## Phase 4 — first registered map and dashboard implemented

The 30-instrument discovery-only run fills the DuckDB edge table at 5m, 1h and
1d, applies global Benjamini–Yekutieli correction and measures an independent
following window: **29,145 cells, 570 stable, 479 first-window significant,
28,096 candidates, zero tradable**. The Map view filters horizons/types/states
and exposes the evidence for each edge. All raw cells and input snapshots persist.

Pearson/Spearman, controlled lagged regression, graphical lasso and
Engle–Granger are wired into this run. Cointegration is unestimable with the
current short windows; lasso links have no calibrated significance. The tested
Hayashi–Yoshida helper is not wired into this synchronous run. The full proposed
Johansen, macro beta/event panels, rolling rebuilds and 150-name coverage remain
extensions, not completed claims. Map stability alone never permits trading.

## Phase 5 — three feeds and observation dashboard verified; phone delivery open

Alpaca IEX, Kraken and Hyperliquid supplied actual complete 180-minute windows.
Raw and normalized live observations are isolated from research. Move, break,
regime, event-window and health detectors share historical features and honor
missingness and known-at timestamps. Lead-fired signals remain disabled because
no accepted strategy exists. Durable deduplication, cooldowns and delivery
receipts are implemented; external sending is opt-in and currently off.

All four views work in Chromium at desktop and phone widths without page errors
or horizontal overflow. API authentication, WebSocket access, path restrictions,
stale capture handling and feed failure isolation are tested. Polling completed
candles provides observation latency; tick streams, IBKR, news/filings and
semantically matched source-disagreement detection remain extensions.

A selected phone destination, its credentials, an authorized delivery test and
an always-on deployment are still required for Phase 5 acceptance.

## Phase 6 — conditional; not eligible

No strategy passed, so paper trading must not begin. Trade comparison and
normalized paper-fill audit tools are implemented/tested, but a Nautilus runner,
IBKR paper integration, independent acceptance and 30 actual trading sessions
remain prerequisites. The current application contains no order-placement route.

## Verification and next unblockers

145 tests, Ruff lint/format and strict mypy pass. The package builds, a Docker
image builds and passes authenticated API checks, browser checks pass, and an
actual backup restore preserves all registry/map records and locked vaults. See [OPERATIONS.md](OPERATIONS.md) for
commands, recovery procedures and honest boundaries of the audit tools.

The next externally dependent steps are the persistent host/channel selection,
a usable authorized CFD/independent-data source with verified metadata, and
independent review/cost evidence. Missing provider history and elapsed paper
sessions cannot be replaced with generated observations or weaker thresholds.

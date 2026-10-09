# Data and research runbook

## Local development

Run `uv sync --locked` and `make check` in the repository root. Installation uses
the repository lockfile; `uv run --frozen` will not rewrite it. No service or
credentials are required for offline tests. `uv build --no-sources` packages the
CLI. The sample universe is repository configuration, not bundled wheel data;
pass `--universe /path/to/universe.yaml` when running outside the checkout.

For native engine smoke tests, preregistration, run reports and the one-use
holdout workflow, see [RESEARCH.md](RESEARCH.md). No API key is needed to test
the engine or use the already captured crypto history. Preserve research
`registry.duckdb` and input snapshots when backing up data; the registry cannot
be recreated from the coverage catalog.

The seven-experiment campaign is documented in [PHASE3.md](PHASE3.md). Open
`data/reports/phase3/index.html` locally; no web server is needed. `suite-report`
reads saved evidence only. `suite-run` resumes by default, while `--retry-blocked`
explicitly creates new attempts after missing inputs arrive. Registered source
and metadata changes require a new revision, never editing past records.

For the resumable 29-equity Alpaca queue and its credential-propagation diagnosis,
see [EQUITY_BACKFILL.md](EQUITY_BACKFILL.md). The helper uses the registered
discovery window and starts no strategy runs automatically.

## First capture and coverage

```bash
uv run --frozen xasset record --symbols SHELL_UK --days 10
uv run --frozen xasset qc --symbols SHELL_UK
uv run --frozen xasset catalog
uv run --frozen xasset health --symbols SHELL_UK --sessions 5
```

Read both the exit code and JSON. Check `runs/latest.json`: a run must have
`status: ok`, nonzero accepted rows, raw payload paths, and catalog coverage.
An idempotent repeat can have zero *added* rows with nonzero *accepted* rows.
Use `--days 14` or up to `--days 30` to collect additional completed sessions
within the source's retention window. Failures for one symbol do not erase
successful captures for other symbols.

`health` evaluates completed sessions as of now minus 20 minutes. A weekend does
not require weekend bars. `--as-of 2026-10-08T00:00:00Z` selects an explicit
settled cutoff. Dates must include a timezone. The report lists missing counts
and the first missing timestamp for each session. Missing prints may reflect
no trades or a halt, not necessarily a network failure; investigate source
evidence. Full calendar coverage alone is not proof of data correctness.

## Persistent daily recording

Run on an always-on POSIX host with persistent local storage. Do not assume a
  cloud development task will run every day or preserve running processes after
publication. `scripts/nightly.sh` is a finite job, not a daemon. It preserves
failure exit codes and runs health even if ingestion fails.

After verifying the manual capture, install a job using the host's scheduler.
For example, with a **UTC-configured host**, set absolute paths for your machine:

```cron
PATH=/usr/local/bin:/usr/bin:/bin:/home/YOUR_USER/.local/bin
XASSET_DATA_DIR=/srv/xasset/data
10 0 * * * cd /srv/xasset && bash scripts/nightly.sh >> /srv/xasset/recorder.log 2>&1
```

Create the directories with the host user's ownership first, install the locked
environment, and ensure `uv` is on the scheduler's PATH. The futures pilot
deliberately reports unknown coverage, so the full-universe nightly job exits
nonzero until session rules are implemented. A selected-equity `health` command
can verify the initial five-session target separately. Do not suppress failed
statuses to simulate readiness. Add log rotation and host alerts when deploying.

No cron entry or remote service has been installed by the current implementation.
No Telegram, Discord, email, or other outbound alerts are configured.

## Recovery

- Network `ProxyError: 403`: allow `query1.finance.yahoo.com` in the environment's
  network policy, save settings, then retry. Saved drafts do not apply runtime
  changes. Other Yahoo 401/403/429 responses may be provider restrictions; do not
  disable TLS verification or invent credentials.
- Empty or failed request: retain the run manifest; check raw payloads if present.
  Bounded retries apply to 429 and transient server errors. Transport failures
  fail the run and can be retried by the next scheduled overlapping run.
- Interrupted job: rerun the same window. Already published monthly partitions
  are valid files; the catalog can be refreshed with `xasset catalog`. A manifest
  left `running` denotes an interrupted job, not success. OS locks are released
  when the process exits; the presence of `.writer.lock` alone is harmless.
- Catalog failure: Parquet and raw responses are authoritative. Fix the reported
  problem and rebuild the catalog; never delete data to obtain a green status.
- Concurrent recorders: one writer per data root. Keep the store on a filesystem
  with POSIX locking and atomic rename; object storage is not supported yet.

Back up raw files, Parquet, and run manifests together. Preserve provenance and
provider revisions. Avoid importing Yahoo front-month data as per-contract bars.

## Historical ingestion and derived bars

Run bounded backfills explicitly. The following public-source commands have
been exercised in the current environment:

```bash
uv run --frozen xasset ingest --source binance \
  --symbols BTC_USDT ETH_USDT BTC_USDT_PERP \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z
uv run --frozen xasset ingest --source dukascopy --symbols EURUSD XAUUSD \
  --start 2026-09-01T12:00:00Z --end 2026-09-01T13:00:00Z
uv run --frozen xasset qc --universe config/history.yaml \
  --symbols BTC_USDT ETH_USDT BTC_USDT_PERP EURUSD XAUUSD
uv run --frozen xasset build --universe config/history.yaml \
  --symbols BTC_USDT ETH_USDT BTC_USDT_PERP --freq 5m
uv run --frozen xasset audit --universe config/history.yaml \
  --symbols BTC_USDT ETH_USDT BTC_USDT_PERP EURUSD XAUUSD \
  --output data/reports/history-audit.json
```

Historical start is an opening instant (exclusive of a bar ending exactly there),
and end is the last included bar end. Binance supports only completed months;
Dukascopy ranges must be whole UTC hours, up to 31 days per run. Public downloads
keep TLS verification enabled; checksum mismatch rejects the archive. No paid
data is requested. Historical run evidence is in `runs/latest-ingest.json` and
the immutable run-named manifests. Configuration metadata records symbol, feed,
scale, and priority without credentials.

Use `--freq 1h` or `--freq 1d` for the other tested aggregates. Regular-session
windows reset at each session/lunch break. A final short session bucket is valid
only if all its scheduled minutes exist. Crypto uses UTC boundaries. Buckets
with a gap or source switch are omitted, never filled; derived files must not be
treated as complete without checking their timestamps. Unknown FX/CFD and
futures sessions are rejected until verified rules are configured.

`store.reader.load(root, instruments, start, end)` produces a common minute grid
with `observed`, `session_open`, `stale`, `last_observed_at`, and `age_seconds`.
Prices remain null at missing minutes. Unknown sessions have null open/stale
status, so they cannot be mistaken for healthy coverage.

## Alpaca and independent-source checks

Set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` securely in environment settings for
`data.alpaca.markets`, then save/apply the configuration. Both credential bindings are present and historical IEX/SIP capture is verified. Do not enter them in chat or commit `.env`.
Once bound, validate a small equity window before scaling:

```bash
uv run --frozen xasset ingest --source alpaca --symbols SPY XLE \
  --start 2026-10-06T00:00:00Z --end 2026-10-07T00:00:00Z
uv run --frozen xasset record --universe config/history.yaml --symbols SPY XLE --days 7
uv run --frozen xasset reconcile --universe config/history.yaml --symbols SPY XLE \
  --left alpaca --right yahoo --start 2026-10-06T00:00:00Z \
  --end 2026-10-07T00:00:00Z --output data/reports/equity-reconciliation.json
```

Historical Alpaca access is verified; these small-window commands remain useful diagnostics. The default IEX feed can
differ from consolidated Yahoo observations; review semantics as well as prices.
Reconciliation requires at least 95% overlap and all matched OHLC within 0.5% by
default. It fails on no overlap, duplicate data, or identical providers. Tolerances
are explicit research inputs, not knobs to increase until a check passes. This
does not replace the planned Alpaca/Massive and Dukascopy/HistData comparisons.

Anomaly reports expose large close changes, possible split-like ratios, and
consecutive zero-volume runs. They never infer a split or silently repair data.
The nightly script now saves `quality-latest.json` as well as coverage health.

## Explicit adjustments and futures rolls

`normalize.adjust.adjust_splits(bars, actions, as_of)` accepts verified `Split`
events with `symbol`, `effective_at`, `known_at`, and `new_shares_per_old`. It
adjusts only actions effective and known by the cutoff, clips future bars,
rescales share volume, and rejects already-adjusted input. Supply the research
window's actual as-of time. Dividend/total-return adjustments are not implemented.

`normalize.futures_roll.continuous(contracts, schedule, root, as_of)` accepts a
mapping of explicit contract IDs to canonical bar frames and `Roll` events with
`contract`, `effective_at`, and `known_at`. New contracts supply minutes opening
at/after the roll; a bar ending exactly at the switch still belongs to the old
contract. Output adds `contract_symbol` and flags the first new-contract bar.
No backward adjustment is applied. Missing contracts, empty scheduled intervals,
post-hoc schedules, and Yahoo/CFD proxies are rejected. Real contract data and
verified schedules are still required before operational use.

To use return features on this derived output, retain the contract column in
your audit dataset and pass `output.select(BAR_SCHEMA.names())` to the shared
feature function. The roll flag suppresses the synthetic jump return and resets
the volatility warmup. These APIs are tested transformations, not a live futures
data source or a complete corporate-action ledger.

## Map, monitor, backups and deployment

See [OPERATIONS.md](OPERATIONS.md) for the four-view dashboard, three-feed monitor,
alert controls, Docker/systemd deployment, verified backup/restore and conditional
paper-fill audits. Existing campaign commands require their archived implementation
as described in [PHASE3.md](PHASE3.md). The current checkout must use a new
registration for new research.

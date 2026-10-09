# Relationship map, monitor and deployment

The application observes markets. It has no order-placement route. Research
acceptance remains separate from descriptive map results and monitor alerts.
The original project acceptance requirements remain in [ROADMAP.md](ROADMAP.md).

## Start locally

```bash
uv sync --locked
uv run --frozen xasset-app monitor --config config/monitor.yaml --once
uv run --frozen xasset-app serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` on the same machine. Omit `--once` for the monitor
daemon in a separate terminal. A finite cycle exits nonzero on missing, stale or
failed feeds. The daemon retries after its configured interval. Keep the data
directory on a local POSIX filesystem; concurrent writers use advisory locks.

The four dashboard views show the relationship graph and evidence, completed
prices and alerts, source health and coverage, and saved experiment verdicts.
The dashboard uses a small bundled JavaScript/SVG frontend rather than a second
React build. It uses WebSocket updates with HTTP polling as a fallback. Stale
capture timestamps override previously healthy feed badges. This is a local
application, not a published website.

## Registered map

```bash
uv run --frozen xasset-app map config/map.yaml
```

`cross-asset-map-v1` declares 30 instruments, July 1–September 12 discovery data,
an August 7 split, 5m/1h/1d horizons, 1–12 intraday lags and 1–5 daily lags.
It is bounded before every referenced research vault. Registration records the
instrument definitions and implementation hash before observations are loaded.
It persists the exact input snapshot, every tested cell and an attempt record.
Completed runs resume their saved result; interrupted runs require a new ID.

All pair/horizon/lag tests participate in one Benjamini–Yekutieli correction in
each window, including unestimable cells as p=1. Intraday inference clusters
by date; daily inference uses HAC errors. A stable edge must survive correction
in both windows, retain its sign and at least half its first-window strength.
Intraday returns retain missing clock intervals. Daily crypto observations are
aligned to the same XNYS close-to-close intervals as equities, with complete
minute coverage between reference closes. No prices are forward filled.

Engle–Granger requires 60 observations in each window; this short campaign
cannot support it. Graphical-lasso links remain candidates because their
selection does not have a calibrated p-value. The tested Hayashi–Yoshida helper
is not part of this synchronous-grid run. Johansen, macro-driver beta panels,
event-response maps, rolling rebuilds and the proposed 150-name universe remain
extensions. The present graph does not claim those capabilities. Stable map
edges are never automatically promoted to tradable edges.

Outputs: `data/relationships.duckdb`, `data/maps/cross-asset-map-v1/`, and the
atomic `data/maps/latest.json` consumed by the dashboard.

## Live capture and alert delivery

`config/monitor.yaml` selects Alpaca IEX SPY, Kraken BTC/USD spot and Hyperliquid
BTC/USDC perpetuals. Sources poll bounded completed one-minute candles. This
provides three independent feeds; it is not a tick-level or live-futures feed.
Live captures are isolated under `data/live/`, with raw receipts and normalized
partitions. They cannot alter frozen research snapshots.

Implemented detectors are move, residual break, correlation regime change,
known scheduled-event windows, and data health. Features reuse the historical
return/volatility implementation. Missing minutes suppress dependent signals.
Lead-fired detection remains disabled because no strategy has cleared the
acceptance gate. News/filings, IBKR subscriptions and source-disagreement alerts
between semantically equivalent instruments remain outstanding.

Every event is stored in `data/monitor.duckdb` with a stable ID and per-kind,
per-symbol cooldown. Sending defaults to `delivery_enabled: false`; merely
supplying credentials cannot send a message. After the user chooses a recipient
and authorizes delivery, set `alert_mode: telegram` or `discord` and
`delivery_enabled: true`, and provide the corresponding environment variables
listed in `.env.example`. Expired queued alerts are skipped. A timeout creates
an uncertain receipt and is not retried automatically. Review it before any
manual resend. No phone notification has been sent from this workspace.

## Persistent host

The development workspace is not an always-on host. Deployment requires the
chosen PC/server, persistent storage and its own credentials. A credential
binding in the cloud development proxy is not an exported host secret.

Docker Compose runs the monitor and dashboard with restart policies, a read-only
application filesystem, an unprivileged user and health probes. The dashboard
publishes only on the host's loopback address. Choose a private random
`XASSET_DASHBOARD_TOKEN`; the app exchanges it for an HttpOnly session cookie.
Keep remote access behind an authenticated SSH/VPN tunnel or HTTPS proxy.

```bash
# Export credentials and XASSET_DASHBOARD_TOKEN securely in the host shell.
# Set XASSET_UID/GID if the data directory is owned by a user other than 1000.
mkdir -p data
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail 30 monitor
```

If the build host uses a TLS inspection proxy, pass its combined trust bundle
as a BuildKit secret, never copy it into the final image:

```bash
docker build --secret id=proxy_ca,src=/path/to/combined-ca.pem -t xasset:local .
```

For non-container installations, templates in `docs/deployment/` run the monitor
and the finite nightly recorder with systemd. Adjust `/srv/xasset`, the service
user and `/etc/xasset/runtime.env`, install the locked virtualenv, then enable
the monitor service and recorder timer. The timer is explicitly UTC and catches
missed runs after reboot. The nightly script uses the installed `.venv/bin/xasset`.
Do not run the systemd monitor and Compose monitor against the same store.
The pilot recorder can still fail health because known upstream gaps and
unverified futures sessions remain; those failures must remain visible.

No scheduler or persistent remote deployment is installed yet.

## Backup and recovery

Stop the monitor, recorder and research/map jobs first. Backups reject active
monitor/map locks and acquire both live-store and root writer locks. They omit
temporary/lock files, reject symlinks, preserve raw artifacts, frozen code,
registries and snapshots, and verify every archive member. Keep archive and
manifest together. Checksums detect corruption; they are not a signature.
Store credentials separately in the host's secret manager.

```bash
uv run --frozen python scripts/backup.py create --data-dir data /backup/xasset.tar.gz
uv run --frozen python scripts/backup.py verify /backup/xasset.tar.gz
uv run --frozen python scripts/backup.py restore /backup/xasset.tar.gz /srv/xasset-restored
```

Restore requires a new destination and verifies contents before publishing it.
Point a stopped installation at that restored directory, run its health checks,
then restart services. Move backups off the development workspace for durability.

Verification and restore also work with standalone Python 3.12 on Windows;
the backup creation command and application writers use POSIX locking. To restore
the saved Git checkpoint without installing application dependencies, run
`py -3.12 scripts\backup.py restore-checkpoint artifacts\checkpoint-20261009 data`
from the clone's root. This joins the archive parts in a temporary directory,
checks their hashes, restores into a new destination and removes the temporary
archive. Checkpoint files retain their original bytes on Windows via
`.gitattributes`, so automatic line-ending conversion cannot invalidate hashes.

## Conditional reconciliation and paper observation

`xasset-app reconcile-trades native.json external.json --output report.json`
compares two arrays of normalized `Trade` records (same canonical trade IDs).
It checks timing, fills, costs, quantities, exit reasons and aggregate PnL. An
empty comparison fails. A match is not proof of an independent engine run and
cannot accept a strategy. A Nautilus runner, daily-equity reconciliation and
independent audit still have to be supplied for a qualifying result.

`xasset-app paper-audit fills.csv --symbols SPY --output paper.json` accepts a
normalized single-account IBKR paper export with columns `fill_id,account,symbol,
session,side,units,reference_price,fill_price,commission`. It rejects real account
IDs, undeclared symbols, duplicate fills, non-session dates and invalid numbers.
At least 30 distinct filled sessions are required for review; the tool does not
verify broker provenance, place orders or approve a strategy. No paper study is
eligible until a strategy passes. Thirty real sessions cannot be synthesized.


## Saved verification

`data/reports/completion/verification.json` records the passing 145-test suite,
Ruff/mypy results, browser checks, authenticated container smoke test and actual
backup restore. The restored databases contained 15 research families, 636
registered candidates, 29,145 map cells and zero opened vaults. Desktop/mobile
screenshots are in the same directory. QA servers and containers are stopped
after verification; this is not a persistent deployment.

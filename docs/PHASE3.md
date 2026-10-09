# Phase 3 — first experiment campaign

The seven original hypotheses now have immutable designs, a batch runner, input
readiness checks and local HTML/Markdown pages. Their **first executable slices**
are implemented. Each page names deferred horizons/models; Phase 3's seven
statistical verdicts are not all complete. The current SIP revision has one
rejected result and six blocked designs.

## Current SIP revision — October 9, 2026 UTC

The official [Alpaca FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)
confirms free historical SIP access when the requested end is at least 15
minutes old. Before capture or outcomes, `phase3-sip-experiments-v2` registered
all 316 candidates again with separate `_SIP` instrument IDs. Dates, thresholds,
parameter grids and costs are unchanged. The registry now contains 636 declared
candidates including the earlier smoke and IEX designs. No IEX data was replaced.

All 87 SIP chunks completed with 576,013 equity bars; all 29 symbols meet the
95% coverage threshold. Raw checksums, pagination, timestamp bounds and normalized
fingerprints pass the offline audit. The source/acquisition decision is saved
in `data/engineering/sip-acquisition-registration.json`.

The bitcoin family run `f02d3d20496b4e308ad1b9d3b6813aa2` is **rejected**:
20 trades over 60 daily observations, +320.46 at base costs and +240.95 at doubled
costs on an initial 100,000. Daily t-statistics are approximately 0.487 and 0.368;
DSR probabilities 0.0075 and 0.0053; PBO 0.114 and 0.186. Significance,
selection-adjusted statistics, fold concentration and delay checks fail.
Positive net PnL does not establish an accepted edge. Independent data and cost
evidence are also pending.

The other six designs remain blocked on verified CFD data/metadata/calendars;
the EIA design also needs a point-in-time event schedule. Dukascopy now returns
`Too Many Requests (Bot blocked)` and points to its requester-pays AWS export.
No further automated requests or paid acquisitions were attempted.

Current reports are in `data/reports/completion/sip-campaign/`; the dashboard
reads its saved summary from `data/reports/completion/campaign-status.json`.
The SIP audit is `data/reports/completion/sip-audit.{html,json,csv}`. All original
IEX records and every vault remain intact and locked.

### Frozen research implementation

The original engine hashes every Python file in its source package. The new
map/monitor files therefore change the current implementation fingerprint.
To read/resume the **already registered** designs with their exact implementation,
use the archived source package that was saved before this work:

```bash
export PYTHONPATH="$PWD/data/engineering/revisions/16607202dff10818dac47b5bfc2ca0906f29744d51dbeb90b77b6d630d2ad872"
uv run --frozen xasset research suite-report phase3-sip-experiments-v2 \
  --output data/reports/completion/sip-campaign
unset PYTHONPATH
```

This archive is part of the data backup. Do not alter old registrations to match
the current code. New research implementations require new registrations and
remain subject to the cumulative trial penalty.

## Historical IEX status — October 8, 2026

All designs were registered before reading discovery data. The 316 candidates
join four previous crypto smoke candidates, so each initial attempt records a
global trial count of **320**. All seven attempts are **blocked / not evaluated**.
These are readiness outcomes, not positive or negative performance verdicts.
No vault was opened and no strategy is accepted.

| Experiment | Candidates | Executable slice | Main missing inputs |
| --- | ---: | --- | --- |
| Oil → energy / airlines | 64 | 5m signal; long energy after oil rises or airlines after oil falls; 15/60m holds | Equity history; verified oil mappings, scales, sessions and quotes |
| Overnight indices → sectors | 88 | Prior US close to open; selected long sector; 60m or session-close exit | Equity history; verified overnight index CFD data |
| Bitcoin → crypto equities | 16 | Complete overnight/weekend BTC window; COIN/MSTR/MARA/RIOT at US open | Alpaca equity history; BTC discovery coverage is complete |
| Gold → miners | 8 | 5m gold signal; GDX/NEM, 15/60m holds | Equity history; gold history and sessions |
| Rates/dollar → sectors | 48 | Hourly TLT/IEF/EURUSD signal; both registered directions; 60m hold | Equity history; EURUSD history and sessions |
| EIA inventory response | 40 | Observe 5/15m oil response; enter once; 15/30m hold | Equity/oil history; verified point-in-time event calendar |
| Regional sessions → US open | 52 | Latest Asian/current European reference cash-session return; US session-close exit | Equity history; verified regional CFD mappings, scales and data |

After registration, public July and August BTC archives were downloaded and
checksum-verified: **89,280 additional minute bars**. BTC now has 132,480 stored
July–September bars; structural QC passes without warnings. The campaign's
105,594-minute BTC discovery window has 100% coverage. Alpaca credentials remain
intentionally deferred. No equity data, event times or successful verdicts were
invented.

Update, October 8 (America/Toronto): the new environment received both credential
bindings and authenticated historical requests succeeded. All 87 registered
Alpaca IEX chunks are downloaded: 501,039 equity minute bars for 29 symbols. An
offline audit verified all 87 raw response hashes, terminal pagination, bounded
raw/normalized timestamp agreement and saved data fingerprints. Structural QC
has no errors or warnings. Resume reuses all 87 chunks without network calls.

Eleven symbols meet the frozen 95% coverage requirement; 18 do not. The
Bitcoin-equities design remains blocked by COIN (73.82%) and MSTR (93.94%); MARA
(96.62%) and RIOT (98.45%) meet coverage. All seven experiments remain blocked,
so no new research attempts were started. The existing attempt pages retain
their historical evidence. See the current
[equity audit](../data/reports/phase3/equity-audit.html),
[session gap CSV](../data/reports/phase3/equity-audit.csv), and
[backfill handoff](EQUITY_BACKFILL.md). No thresholds, feeds or candidate sets
were changed, and every campaign vault remains locked.

## Reports and commands

Local artifacts:

- `data/reports/phase3/index.html` and `index.md`: seven-page index.
- `data/reports/phase3/summary.json`: statuses, verdicts and blockers.
- `data/reports/phase3/required-inputs.json`: deduplicated discovery-only input ranges.
- `data/reports/phase3/equity-audit.{html,json,csv}`: current input readiness,
  raw evidence checks and 1,479 symbol/session coverage rows, separate from trials.
- `data/reports/phase3/equity-resume-verification.json`: offline resume evidence.
- `data/registry.duckdb`: frozen designs and all attempts.

```bash
# Already registered here; identical registration is idempotent.
uv run --frozen xasset research suite-register config/phase3.yaml \
  --universe config/phase3-universe.yaml --costs config/costs.yaml

# Resume: reuse existing attempts, including blocked/failed attempts.
uv run --frozen xasset research suite-run phase3-first-experiments-v1

# Report generation reads saved evidence; it starts no trials.
uv run --frozen xasset research suite-report phase3-first-experiments-v1 \
  --output data/reports/phase3

# Only after missing data arrives: explicitly retry blocked attempts.
uv run --frozen xasset research suite-run phase3-first-experiments-v1 --retry-blocked
```

`suite-run` exits 1 if any attempt is blocked, failed or still running. Zero means
engine executions completed, not that their research gates passed. Completed
attempts are never rerun by `--retry-blocked`. Each explicit retry increases the
selection penalty. Abandon an interrupted `running` attempt with a reason before
an individual rerun.

Suite and family definitions are immutable. Source-code changes, verified
instrument-metadata revisions, event-calendar additions or hypothesis changes
require new family/suite revisions. Preserve prior attempts and count the new
candidates. Adding bars for an unchanged registered source/range can resolve a
data blocker without changing the design. Do not delete registry history.

## Data and signal boundaries

The registered range is July 1–October 1, 2026. Discovery ends September 12 at
07:54 UTC, followed by a 390-minute embargo and a September 12, 14:24 UTC vault
boundary. These are canonical data timestamps. Training windows are 14 days;
OOS folds are seven days. The shared gate thresholds are unchanged. Required
coverage is 95% of scheduled minutes per instrument; unknown coverage blocks.

Each symbol has a frozen exact source. Reads use provider-specific partitions
and cannot silently substitute canonical Yahoo observations. Imported offline
history works once stored; credentials are not an execution requirement. A Tier A
label describes the intended source, not completed acquisition or independent QC.

New CFD symbol/scale mappings in `phase3-universe.yaml` are **unverified planning
metadata**. Do not ingest those symbols until verified against the provider.
Registered prerequisites keep those experiments blocked. XTKS/XETR are reference
cash-session windows, not full broker CFD calendars. The original `GOLD` label
is excluded until Barrick's point-in-time ticker history is resolved.

- Lead/lag quote drivers require complete UTC minute buckets, both quote sides
  and one source. No exchange volume is invented and no derivative position opens.
- Overnight signals require every driver minute from the previous US close to
  the opening instant. Weekend crypto works; missing/closed quote windows cancel
  signals instead of carrying stale prices.
- Regional signals use the latest eligible reference session, clipping Europe
  at the US opening instant. Holidays, lunch breaks and DST apply. A regional
  close older than one day is discarded.
- Event signals require a preregistered schedule known by release time, then a
  complete 5/15-minute observed price response. This is not an inventory-surprise
  model. The actual campaign calendar remains empty and blocked.
- Equity fills use regular-session bars only. Positions close by their time limit
  or actual session close, including half-days. Missing required closing liquidity
  cannot silently carry a position overnight. Intrabar stop-first rules remain.
- Follower currency must match the cash ledger. Driver returns are dimensionless,
  so differently denominated drivers do not imply position-level FX conversion.

The engine remains long-only and selects one follower/parameter set per fold.
Sector ranking, hedged baskets, short positions, joint-factor models, full 1d
research and additional horizons are deferred explicitly in the configuration.

BTC observations overlap the earlier plumbing smoke data. The workflow guard
cannot establish that previously inspected history is unseen. Future accepted
claims need independent review and appropriate fresh data/access controls.

## Verification and remaining acceptance

All **116 tests**, Ruff and strict mypy pass. Source and wheel packages build
offline. Tests cover weekend windows, DST, partial regional sessions, causal
event timing, quote drivers, half-days, premarket exclusion, event walk-forward
execution, exact sources, readiness invariance to future data, batch resume and
HTML escaping. Hosted CI was not run here; its leakage job includes the new tests.

Update: all 125 tests, Ruff formatting/lint and strict mypy pass after adding
offline evidence auditing. Its tests check raw corruption, missing discovery
rows, future-row invariance, trial preservation and escaped report content.

The SIP revision resolves equity coverage and supplies one negative verdict.
Missing CFD observations and verified metadata still prevent six verdicts. Phase 2 cost calibration and independent data/action evidence remain
unfinished. Any qualifying result also needs independent leakage review and
[cross-engine/exchange-data reconciliation](RECONCILIATION.md). No strategy is
accepted or paper-traded.

Current operations verification: 145 tests, Ruff and strict mypy pass; see
[operations](OPERATIONS.md) for map, monitor, container and browser validation.

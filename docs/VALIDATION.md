# Validation boundary

Current checks validate data and native execution, not an investment edge:

- Strict schema, duplicate keys, finite OHLC, OHLC ordering, nonnegative volume,
  minute timestamps, and immutable raw content hashes.
- Atomic file publication, single-writer exclusion, idempotent overlapping runs,
  provider revision replacement, and recovery from partial symbol failures.
- Exchange holidays, half-days, lunch breaks, DST, and completed-session coverage.
- Known-answer one-minute returns and trailing volatility, no return across
  missing minutes or instrument boundaries, and future truncation/perturbation.
- Binance checksum rejection, timestamp units, fractional volume, and exact CSV
  membership; Dukascopy record bounds, quote integrity, and true midpoint OHLC.
- Alpaca pagination, missing credentials, and protection against mixing feeds.
- Legacy partition migration, independent provider retention, and source priority.
- Reconciliation with insufficient overlap/disagreement, anomaly evidence, and
  missing/stale/closed alignment states.
- Complete session-aware resampling, split availability cutoffs, explicit roll
  boundaries, and suppression of returns at source/contract switches.
- Complete weekend/overnight driver windows, DST, regional session truncation,
  point-in-time event schedules, quote-only driver signals and half-day exits.
- Exact provider requirements, readiness invariant to future data, immutable
  batch registration, resume/retry accounting and escaped experiment pages.

The leakage Action also exercises native causal strategy signals, training-only
selection, immutable snapshots, and holdout boundaries. Known-answer tests cover
next-open entries, same-bar stop/target ambiguity, gaps, missing/zero-volume
fills, cash/fee sizing, daily accounting and exact doubled costs. The engine
reruns with signals delayed by one signal bar; a delay that improves profit or
flips it negative fails the prescribed diagnostic. Local test success does not
mean a GitHub-hosted workflow has executed. Monitor/research parity and independent
cross-engine checks remain pending.

Real validation now includes checksum-verified September crypto archives,
Dukascopy EUR/USD and gold quote hours, and Yahoo captures for all five pilot
instruments. Crypto aggregates and aligned reads have been checked against the
expected row counts. These remain single-source observations: no real independent
provider reconciliation has passed yet. Alpaca is blocked by missing bindings;
Tokyo/Hong Kong source gaps and unknown futures sessions remain explicit.

The native engine is the project's backtest core. Its validation
gate now implements preregistration, a one-use vault, purged/embargoed selection,
1x/2x costs, execution/accounting checks, daily selection statistics and trade/fold
concentration checks. DSR uses an approximation that assumes independent daily
returns and local candidate Sharpe dispersion; CSCV PBO needs at least two
nonconstant candidates and 16 OOS daily observations. Missing evidence stays
null/incomplete, never a fabricated passing score.

Every result has `accepted: false`. Independent data/action evidence is not yet
wired into approval; cost profiles are illustrative. Independent leakage review,
statistical review, one-time holdout evaluation and Nautilus reconciliation must
precede acceptance. Synthetic profits are not evidence of a market edge. The
unavailable original benchmark is retired, not reported as reproduced.

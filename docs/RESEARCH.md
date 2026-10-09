# Native research engine

## Decision and scope

On October 8, 2026, the user replaced the original Hindsight integration with
an engine owned by this project and confirmed the original benchmark inputs
are unavailable. `PROJECT_PLAN.txt` remains an unchanged historical source;
this decision supersedes its engine and benchmark requirements.

See [ENGINE_VALIDATION.md](ENGINE_VALIDATION.md) for the initial engine checkpoint
and BTC/ETH smoke result, and [PHASE3.md](PHASE3.md) for the current campaign,
the rejected SIP bitcoin result, six blocked designs and frozen-code instructions.

Native v1 implements `cross_asset_leadlag`: buy the follower after the driver's
completed return over `lookback` bars exceeds `threshold` in `direction` (+1 or
-1). Both directions produce a long follower position. Only one position is open
at a time. Traded instruments are cash equities/ETFs and spot crypto, with a
single common ledger currency. USD and USDT are distinct. Quote-only drivers can
supply dimensionless returns in other currencies. Derivative positions, shorts,
financing, FX conversion and live orders remain unsupported. Equity corporate
actions and independent data evidence still require review.

Phase 3 adds `session_open` and `event_response`, including regional reference
sessions, preregistered release timestamps and actual equity session-close exits.
It also supports explicit `parameter_sets` for correlated choices such as buying
energy on rising oil and airlines on falling oil. Use either explicit candidates
or a Cartesian `parameter_grid`; both are counted before data reads.

## Offline smoke test

```bash
uv run --frozen xasset research demo > /tmp/xasset-demo.json
uv run --frozen xasset research runs --data-dir data/research-demo
uv run --frozen xasset research report RUN_ID --data-dir data/research-demo \
  --output /tmp/xasset-demo.md
```

The demo creates deterministic synthetic data in a separate fresh directory,
registers its grid, and evaluates discovery at both cost levels. Existing bars
or a registry cause it to refuse overwriting. For another run, choose a new
`--data-dir`. A completed demo exits zero even when its research gate rejects
the synthetic strategy. `purpose: smoke` permanently prevents candidacy.

## Preregister before evaluating real observations

The example uses already captured September BTC/USDT and ETH/USDT spot data.
Its hypothesis and costs are illustrative, and its purpose is explicitly smoke.

```bash
uv run --frozen xasset research register config/research-crypto.yaml \
  --universe config/history.yaml --costs config/costs-crypto.yaml
uv run --frozen xasset research run crypto-native-pilot > /tmp/crypto-native-pilot.json
uv run --frozen xasset research runs --family crypto-native-pilot
uv run --frozen xasset research report RUN_ID --output /tmp/crypto-native-pilot.md
```

Registration reads configuration only. `strategy_revision: current` resolves
to a SHA-256 fingerprint of the installed Python source tree. The full grid,
universe, quote currency, lot sizes, costs and thresholds become immutable under
the family ID. Repeating an identical registration is harmless; changed inputs
need a new family and count as new trials. Engine changes also require a new
registration. Do not rename experiments or delete the registry to evade history.

`registry.duckdb` is durable research evidence, distinct from the rebuildable
`catalog.duckdb`. Back it up along with `research/*/input.parquet` and raw history.
All registered candidates count, including unrun candidates; every extra attempt
adds another conservative trial penalty. Failed data/engine attempts persist.
The run stores its trial-count snapshot, source/spec hashes, a SHA-256 of sorted
logical input rows, the exact input Parquet, trade ledger, daily results and gate.
Interruptions stay `running` until explicitly recorded as abandoned:

```bash
uv run --frozen xasset research abandon RUN_ID --reason "Process interrupted during evaluation"
```

Research `run` exits 0 for a candidate or awaiting-review result, 1 for a completed
rejected/incomplete gate, and 2 for operational failure. None means accepted.

## Time and execution conventions

- A source bar is `[ts_end - 1m, ts_end)`. A signal known at `t` enters at the
  open of the following minute bar `[t, t+1m)`. This assumes zero additional
  processing latency; the explicit signal-delay diagnostic tests sensitivity.
- Driver aggregates must be complete. Features reset at gaps, source switches,
  zero-volume driver bars and roll flags. Missing next follower bars cancel
  entries; they never defer an entry until the next session.
- Whole-lot sizing includes entry costs within the allocation budget. Cash pays
  entry notional/fees and receives exit proceeds minus fees. Costs combine a
  commission floor, percentage/per-unit commissions, half the full spread, and
  adverse slippage. The stress run doubles every component. Fees are recorded
  as cash deductions, separately from reference market fill prices.
- Known opening gaps execute at the actual open. Within a bar, stop wins when
  both stop and target are touched. Intrabar exits are timestamped one microsecond
  before bar end as an accounting convention, not a claimed tick execution time.
- No fills occur without positive traded volume. Missing exits wait for the
  next executable bar; exceeding the registered holding horizon fails the run.
  An unclosed position at a fold boundary also fails. The engine never discards
  an open loss or chooses the last available bar retrospectively to liquidate.
- Daily equity uses observed closes for marking, which does not authorize a
  fill. All UTC dates within OOS folds, including flat days, enter daily returns.
  Portions of the same date across folds are combined before computing returns.

## Selection, statistics, and holdout

The newest 20% of the **declared elapsed-time range** is reserved; this is not
20% of observed rows. Discovery ends another embargo interval before the vault.
The engine receives only authorized rows. It trains on fixed rolling day windows,
allows no training position to cross the training endpoint, waits an embargo at
least as long as maximum holding, and chooses the largest training net PnL at
1x costs. Grid order breaks ties. OOS results never choose fold parameters. The
same winners run at doubled costs; the last discovery fold winner is frozen for
the holdout. A partial final OOS fold is retained and remains visible in reports.

DSR is the Bailey–López de Prado daily Sharpe approximation using skew, kurtosis,
candidate Sharpe dispersion and the registry's global trial count. It assumes
independent daily returns; autocorrelation correction and global search-dispersion
review remain necessary. CSCV PBO splits discovery OOS returns into eight
contiguous blocks, tests all 70 half/half combinations and measures the selected
candidate's OOS rank. It requires at least two nonconstant candidates and 16 days;
ties at the median count against the strategy. Constant/missing candidate series
are not silently dropped. Counterfactual candidate OOS returns serve statistics
only. In the vault, parameters stay fixed, DSR uses discovery dispersion, and PBO
is carried from discovery rather than searching held-out candidates.

Both cost scenarios require daily t >= 2, DSR >= .95, PBO <= .10, at least 20
daily observations, positive net PnL after removing the best ceil(5%) of trades,
and no fold supplying more than half the net profit. Thresholds are preregistered.
The signal-delay check requires lower but nonnegative profit after delaying
signals by one signal bar. These conditions are mechanical screens, not proof
of predictive value.

Once the latest discovery attempt qualifies, an explicit audited command can
release the holdout:

```bash
uv run --frozen xasset research vault-open FAMILY --discovery-run RUN_ID \
  --reason "Discovery evidence reviewed and frozen for final evaluation"
uv run --frozen xasset research run FAMILY --phase vault
```

The reservation is consumed even if evaluation fails. Discovery is permanently
closed after release. No vault has been opened by this implementation work.
This is a workflow control, not encryption or access isolation: an owner can
read raw Parquet, query DuckDB, or bypass application APIs. It cannot prove that
previously viewed historical data is unseen. A truly sealed dataset needs
separate access controls and fresh observations.

## Acceptance still pending

Current costs are explicitly uncalibrated, and independent price, corporate-action
and session/coverage evidence is not yet connected to the research approval gate.
That check stays incomplete even when structural QC passes. Candidate states are
therefore unavailable until that integration and evidence exist. Independent
look-ahead review, statistical review and a Nautilus reconciliation also remain
required. All reports retain `accepted: false`; no benchmark reproduction,
profitable market relationship, or trading readiness is claimed.

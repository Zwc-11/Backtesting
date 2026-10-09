# Notebook studies (experimental strategies)

The experimental notebook defines model-based strategies: a proposed signal, a
forecast with and without it, and a trade rule. They are tested as registered
**studies** with purged, nested walk-forward folds. The notebook's own priority order
is followed: strategies 1, 3, 4 and 8 are implemented; 2 and 5 are next; 6, 7, 9 and 10
need order-book or event-level data that no public archive provides.

| Study | Strategies | Data | Folds | Holdout |
|---|---|---|---|---|
| `notebook-crypto-daily` | n08 upper-tail escape, n03 downside separation, n01 missing breakdown | daily Binance USDT spot, 2019–2026, including delisted pairs | yearly tests from 2022 | the last 20% (from 26 May 2025) |
| `notebook-crypto-intraday` | n04 directional recovery clocks | one-minute archives of 28 spot coins, 2025–2026 | quarterly tests from Oct 2025 | the last 20% |

## Data

- `xasset study ingest-daily` downloads daily klines for every USDT pair in Binance's
  exchange information, which still lists delisted pairs (status `BREAK`); the public
  market-data API serves their history. Stablecoins, fiat, leveraged and wrapped
  tokens, tokenized equities and dollar-pegged series are excluded by type.
- A ticker whose trading stopped for more than a week becomes a new instrument after
  the gap (`LUNAUSDT` and `LUNAUSDT~2`), so nothing is carried from a collapsed coin to
  the coin that later reused its ticker.
- Eligibility each day: listed at least 90 days (in the current trading history),
  trailing 30-day median notional of at least 5 million USDT, and in the top 100 by
  that liquidity. Everything uses data up to that day only.
- n04 also reads the one-minute archives under `data/lab/bars/binance-spot/` for
  2025-01 to 2026-09 (`xasset lab ingest-universe config/lab/crypto-universe.yaml
  --start 2025-01-01T00:00Z --end 2026-10-01T00:00Z`). The archives are
  checksum-verified, so a fresh download reproduces the registered study exactly.

## Method

For each test fold:

1. **Selection inside training.** Models are fitted on rows whose outcome window ends
   before the validation slice (the last quarter of training). The ridge penalty
   (0.001, 0.01, 0.1) is chosen by validation error and the signal threshold quantile
   (0.5, 0.7, 0.8, 0.9) by the validation net return of the trade rule.
2. **Refit and apply.** Models are refitted on every training row whose outcome ends
   before the fold (purge = horizon + 1 days) and applied unchanged to the fold.
3. **Incremental value.** The return model with the signal (m1) is compared with the
   same model on the ordinary controls only (m0): recent returns, volatility, beta,
   range, liquidity, market and index moves, weekend.
4. **Trade rule.** Enter long when the strategy's own condition holds, the signal is at
   or above the threshold, m1 beats m0, and m1 minus round-trip costs exceeds the
   training optimism allowance (how far m1 overstated realized returns in training).
5. **Matched baseline.** Each day, the same number of names ranked by m0 alone, traded
   with the same rule, is reported next to every strategy.

Execution: decided after a UTC day closes, entered at the next open moved by half
spread and impact, 0.10% fee per side; ten equal-weight positions per strategy; at most
2% of a coin's median daily notional. Stress scenarios: double costs, and entry one
day later. Daily bars cannot order a target and a stop touched on the same day: the
stop is taken and the target is recorded as a bound.

Reported per strategy: trades, win rate, net P&L, HAC t-statistic on daily P&L, max-T
adjusted p across all sleeves (baselines included), drawdown, five-session windows
reaching +10%, out-of-sample incremental R², rank correlation, the residualized slope
θ with a 20-day block-bootstrap interval, and the notebook's rejection checks
(calibration skill of rare-event and breakdown probabilities, correlation of the
signal with volatility, beta and trend, results without unfinished excursions).

## Known simplifications

- n08's tail signal and payoff come from first-stage classifiers; the second-stage
  return models are fitted on those classifiers' in-sample values for training rows
  (no cross-fitting). Test folds stay strictly out of sample, so reported results
  are unbiased for this procedure, but the second stage may lean slightly more on
  the signal than cross-fitting would allow.
- n04 matches excursions by size and the coin's own volatility; time of day and
  market direction are absorbed by comparing each coin with its own reference window
  rather than by an explicit duration model with those covariates.
- Spread and impact tiers are assumptions. Daily bars cannot see intraday liquidity,
  so the strategies trade the next open with those assumed costs.

## Commands

```bash
uv run --frozen xasset study ingest-daily --start 2019-01-01
uv run --frozen xasset study register config/lab/notebook-crypto-daily.yaml
uv run --frozen xasset study run config/lab/notebook-crypto-daily.yaml
uv run --frozen xasset study holdout-open notebook-crypto-daily --reason "…"
uv run --frozen xasset study run config/lab/notebook-crypto-daily.yaml --phase holdout
```

Registration freezes the study file, a fingerprint of every price the study window
can read, and the notebook code. Extending the daily data with later days keeps the
fingerprint; any change inside the window stops the run until a new study ID is
registered.

# Native engine validation — October 8, 2026

`make check` passes: 99 tests, Ruff and strict mypy. `uv build --no-sources`
builds both source and wheel packages. Two existing upstream calendar/NumPy
deprecation warnings remain non-fatal. GitHub-hosted CI was not run in this task.

The new tests cover exact next-open fills and cash balances, stop-first ambiguity,
opening gaps, missing/zero-volume entries and exits, fee-inclusive lot sizing,
doubled costs, signal truncation and future perturbation, training-only selection,
embargo bounds, immutable preregistration, failed-trial accounting, a one-use
vault, logical-row fingerprint invariance and tampered-result rejection.

A deterministic synthetic CLI run completed all stages. Its research gate
rejected the signal-delay check and retained missing statistical/data evidence;
it did not become a candidate despite positive synthetic PnL.

## Existing BTC/ETH history smoke run

The predefined `config/research-crypto.yaml` grid was registered before this run.
This is a plumbing smoke experiment with illustrative costs, not a strategy
recommendation or a reproduction of the retired benchmark.

- Family: `crypto-native-pilot`; four registered candidates; one discovery attempt.
- Run: `830e3e669aba465a933da292f1261af7`; execution status `completed`.
- Input: 69,000 minute bars, September 1 through September 24 at 23:00 UTC.
- OOS daily observations: 21; 161 completed trades in each cost scenario.
- Gate: **rejected**, `accepted: false`; vault remains unopened.

| Scenario | Net PnL (USDT) | Daily t | DSR probability | Discovery PBO |
| --- | ---: | ---: | ---: | ---: |
| 1x illustrative costs | -4,416.84 | -6.1802 | 0 | 0.0143 |
| 2x illustrative costs | -8,321.15 | -6.1606 | 0 | 0 |

Low PBO does not rescue a losing strategy. Both scenarios fail net profit,
trimmed profit, daily t, DSR and signal-delay requirements. Cost calibration,
independent data evidence and smoke purpose also prevent candidacy. No settings
were changed to improve the result and no held-out data was evaluated.

Local artifacts:

- `data/registry.duckdb` contains the immutable definition and full run JSON.
- `data/research/830e3e669aba465a933da292f1261af7/input.parquet` is the exact input.
- `data/reports/crypto-native-pilot.md` is the readable gate report.

Fingerprints:

```text
source  3ba3a18374647adff65f44e64ff1601d6cd52463c18b5e5bea5907a0f2e651d8
spec    7611ec179c432337e4f7ff3d3e347d9e6afe4a2a12ac16ef5c07fe25fc8681e9
data    aff471380877a7ab1748116828e1b0b9f7503ec262b6e8518f8c2f4602d7a9d6
```

Market files and registry artifacts are ignored by Git and must be preserved
with the environment/data backup. See [RESEARCH.md](RESEARCH.md) for semantics,
supported assets and remaining independent acceptance requirements.

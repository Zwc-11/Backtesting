# Alpaca equity backfill

The registered Phase 3 campaign needs 29 equities. The operational helper uses
the registry's frozen instrument definitions and discovery boundary, dividing
the work into 87 monthly or partial-month chunks. COIN, MARA, MSTR and RIOT come
first because the BTC driver history is already complete.

```bash
uv run --frozen python scripts/backfill_equities.py plan \
  --output data/reports/phase3/equity-backfill-plan.json

# Authenticated in the current environment:
uv run --frozen python scripts/backfill_equities.py run \
  --symbols COIN MARA MSTR RIOT \
  --output data/reports/phase3/equity-backfill-status.json

# Continue with all registered equity symbols; validated chunks are reused.
uv run --frozen python scripts/backfill_equities.py run \
  --output data/reports/phase3/equity-backfill-status.json

# Offline evidence and session-gap audit; does not start a strategy attempt.
uv run --frozen python scripts/backfill_equities.py audit \
  --output data/reports/phase3/equity-audit.json
```

The helper first probes 30 minutes inside the first pending chunk, then downloads
the queue using the existing Alpaca adapter. Authentication or download failure
stops the queue. Successful chunks are checkpointed in
`data/backfills/phase3-first-experiments-v1-alpaca.json`; each references its ingest
manifest and the fingerprint of normalized, bounded source rows. Resume verifies
those rows and the exact registered feed before skipping a chunk. Missing or
changed files trigger a bounded re-download. All requests are historical GETs;
no trading endpoints are used.

The saved plan covers July 1 through September 12, 2026 at 07:54 UTC. No vault
interval is fetched by this helper. Conflicting instrument definitions, different
shared discovery bounds or a released family vault are rejected. The operational
script does not change the registered engine source fingerprint.

Download completion is separate from data coverage. Sparse IEX observations may
still fail the registered 95% scheduled-minute threshold, particularly for less
liquid names. Do not forward-fill prices, lower the threshold, or switch to SIP
to obtain a passing result. A different feed requires an explicitly registered
design and separately identified instrument/store. The helper records current
readiness after downloading but never starts strategy runs automatically.

## Verified capture — October 8, 2026 (America/Toronto)

The resumed environment received both credential bindings. Historical requests
to `data.alpaca.markets` succeeded using the existing proxy and registered IEX
feed. The original authentication blocker is resolved.

All **87 chunks** for **29 equities** are complete, containing **501,039** distinct
regular-session minute bars. The first run downloaded 12 chunks for COIN, MARA,
MSTR and RIOT; the second reused those and downloaded the other 75. A subsequent
resume with a transport that rejects every network request reused all 87 chunks.
Discovery ends September 12 at 07:54 UTC; no vault range was requested.

The offline audit verified 87 raw response checksums, pagination termination,
the exact feed/instrument metadata, normalized checkpoint fingerprints, and
timestamp agreement between raw responses and the bounded store. Structural QC
reports no errors or warnings. This checks integrity, not independent price or
corporate-action accuracy.

Across 51 sessions per symbol, **75,771 scheduled minutes are absent**. The same
minutes are absent in the saved raw IEX responses; the audit found no dropped
normalization rows. This alone cannot distinguish no eligible trades, halts or
feed outages. Only 96 of 1,479 symbol/session combinations are complete; 59 lack
the opening bar, and all have the closing bar.

Eleven symbols meet the frozen 95% aggregate threshold: GDX, HAL, KRE, MARA, OXY,
QQQ, RIOT, SPY, XLE, XLF and XLU. Passing this coverage check does not approve
their prices, execution assumptions or any strategy.

| Bitcoin-equity follower | Stored minutes | Coverage | Frozen requirement |
| --- | ---: | ---: | --- |
| COIN | 14,682 | 73.82% | Not met |
| MARA | 19,218 | 96.62% | Met |
| MSTR | 18,685 | 93.94% | Not met |
| RIOT | 19,582 | 98.45% | Met |

All seven experiments still have input blockers. No new strategy attempt was
started, so trial counts and saved verdicts are unchanged. All seven campaign
vaults remain locked and the registered engine fingerprint is unchanged.

Current evidence:

- [Readable audit](../data/reports/phase3/equity-audit.html)
- [Machine-readable evidence](../data/reports/phase3/equity-audit.json)
- [Session gaps](../data/reports/phase3/equity-audit.csv)
- [Offline resume verification](../data/reports/phase3/equity-resume-verification.json)

The next research step needs a separately registered data design capable of
meeting coverage, plus the CFD/calendar inputs required by the other families.
Repeating identical IEX downloads does not establish that absent minutes exist.
Preserve this campaign and its negative readiness evidence when considering a
new source, universe or observation model; count any new candidates explicitly.

## Historical credential diagnosis — resolved

The user has shown both `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` configured in the
Network secrets UI. In the task that prepared this helper, neither corresponding
variable/placeholder was present. The configuration metadata endpoint also
reported no saved bindings for this attached draft. A read-only historical GET
through the existing HTTPS proxy reached `data.alpaca.markets` and returned HTTP
401 without credential headers. That does **not** establish that the user's saved
keys are invalid; this session had no credentials to test.

That diagnosis applied to the previous task. The new task used the published
configuration successfully; no key re-entry was needed. The current
`equity-backfill-status.json` records download completion and current coverage.

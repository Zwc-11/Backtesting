# Cross-engine and exchange-data reconciliation

No experiment currently qualifies for acceptance reconciliation. The previous
BTC/ETH smoke result was rejected; the SIP bitcoin-equities result is rejected and six SIP designs are input-blocked.
No Nautilus execution or CME confirmation is claimed.

For any qualifying discovery result, freeze and record:

1. Family/run IDs, source/spec/data fingerprints, parameters and both cost scenarios.
2. Native/Nautilus versions, timestamps, sizing, calendars, order latency,
   stop/target ordering and commission/spread/slippage conventions.
3. Trade-level comparison of signal/order/fill times, prices, units, fees,
   gross/net PnL and exit reasons; daily-equity reconciliation.
4. Every discrepancy and its explanation. Parameter changes require a new
   registered design and trial penalty; do not tune to match a favorable result.
5. For CFD-derived findings, confirmation on verified dated exchange contracts
   and roll schedules with explicit identity, multiplier and session differences.
   A Yahoo front-month proxy is not confirmation.
6. Independent look-ahead review and the one-use holdout decision, with references
   to reviewed artifacts. Mechanical screens alone are not accepted verdicts.

Paid data accounts, credits and trading connections have not been used. This is
a pending reconciliation protocol, not a completed audit.


A normalized trade comparator and paper-statement auditor now exist in
`src/xasset/crosscheck/reconcile.py`, with CLI commands documented in
[OPERATIONS.md](OPERATIONS.md). They expose fill/time/cost discrepancies, reject
empty comparisons and enforce a minimum 30-session paper review. Tests include
mismatched costs/timestamps, real-account rejection and incomplete observation.
They are audit utilities, not Nautilus/IBKR executions; no independent rerun or
paper-fill history is claimed.

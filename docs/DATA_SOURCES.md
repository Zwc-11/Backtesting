# Implemented source contracts

## Yahoo forward capture

Unofficial endpoint: `query1.finance.yahoo.com/v8/finance/chart/{symbol}`.
Requests use minute bars, at most seven days per request, and an approximate
30-day retention cap. All five forward-pilot instruments were captured live.
A 20-minute settlement buffer does not prevent later revisions. Null OHLC and
incomplete/off-session minutes are excluded and counted.

Split/dividend events remain in raw JSON but are not automatically applied.
ES=F and CL=F are unadjusted front-month proxies, not explicit contracts.
Calendars model scheduled minutes, not every halt or auction behavior.

## Binance archives

Official [format documentation](https://github.com/binance/binance-public-data).
The endpoint is `data.binance.vision/data/spot/monthly/klines/...` or
`data/futures/um/monthly/klines/...`. Only completed months are requested;
sub-month requests fetch and verify the relevant full archive before slicing.
Missing archives fail rather than implying an empty complete period.

The published `.CHECKSUM` filename and SHA-256 must match the archive. Its ZIP
must contain exactly the expected CSV; download and expansion sizes are bounded.
Spot timestamps changed from milliseconds to microseconds in January 2025;
both formats' minute bounds are checked. USD-M headers are supported. Fractional
base-asset volume is retained. Perpetuals and spot have distinct IDs, and
BTC/USDT is not treated as BTC/USD. The September 2026 pilot was verified live.

## Dukascopy quote history

Endpoint: `datafeed.dukascopy.com/datafeed/{symbol}/{year}/{zero-based-month}/{day}/{hour}h_ticks.bi5`.
LZMA decompresses 20-byte big-endian records: elapsed milliseconds, integer ask,
integer bid, float ask size, float bid size. An explicit instrument price scale
is required: 100,000 for pilot EUR/USD and 1,000 for XAU/USD. Quotes are checked
for ordering, hour bounds, crossed prices, and invalid quoted sizes.

Bid/ask OHLC and tick-by-tick midpoint OHLC are aggregated separately. Quote size
does not become traded volume. Empty hours are recorded in the manifest; missing
minutes are not filled. HTTP errors do not mean market closures. Requests are
serialized with bounded retries; numeric Retry-After delays are respected, and
long delays fail for a later host retry. Runs are capped at 31 days; begin with
small ranges before planning large backfills.

## Alpaca historical equities

Endpoint: `data.alpaca.markets/v2/stocks/{symbol}/bars`. Credentials use
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` on that HTTPS destination. No values enter
source files, manifests, or saved instructions. Both IEX and delayed historical
SIP requests have been authenticated and audited in this workspace.

Requests use raw adjustments, minute bars, explicit `iex` (default) or `sip`,
ascending pagination, and a 10,000-row page limit. Do not choose SIP without the
account entitlement. The October 9 checked Alpaca FAQ permits free historical
SIP requests whose end is at least 15 minutes old; the registered SIP campaign
uses separate instrument IDs and no recent/realtime SIP entitlement is inferred. Repeated pagination tokens fail. IEX is a partial-market
feed; its volume and prints need not equal consolidated observations. Changing
feed requires a separate instrument/store.

## Current Dukascopy access restriction

On October 9, official metadata requests returned `Too Many Requests (Bot blocked)`
and linked to [the export documentation](https://www.dukascopy.com/wiki/en/development/data-export/).
That page describes a requester-pays AWS bucket requiring AWS credentials and
billing. Automated requests were stopped. The earlier two-hour pilot remains
valid stored evidence but does not imply current bulk-download access. Provider
pages and response evidence are saved under `data/engineering/source-documentation/`.

## Live observation feeds

The monitor polls completed minute candles from Alpaca IEX, Kraken's public
OHLC endpoint and Hyperliquid's read-only `candleSnapshot` info request. It never
uses an order API. Actual captures verified all three feeds. Kraken BTC/USD spot
and Hyperliquid BTC/USDC perpetual retain different venue/currency/contract
identities and are not treated as interchangeable price sources. See
[OPERATIONS.md](OPERATIONS.md) for settling, gap and freshness rules.

## Remaining work

Massive/HistData access/import, a provider-backed corporate-action ledger,
IBKR/Databento contracts, and macro/news/live-websocket adapters remain pending.
No paid download or trading endpoint is invoked. The original plan's pricing
and provider promises have not been broadly reverified.

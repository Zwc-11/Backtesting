"""Every supplied strategy, where it can honestly run, and why.

``backtest`` lists books with historical inputs that satisfy the strategy (trade-bar
variants where quotes are absent). ``paper`` lists live paper desks whose feeds carry
the primary version's inputs. A strategy with neither is shown with its blocker.
"""

from __future__ import annotations

from typing import Any

from xasset.lab.strategies import REGISTRY

HANDBOOK = "Ten trading strategy specifications (Word)"
NOTEBOOK = "Ten experimental trading strategies (PDF)"

ENTRIES: list[dict[str, Any]] = [
    {
        "id": "h01",
        "doc": HANDBOOK,
        "number": 1,
        "priority": True,
        "idea": "An asset that holds up while the market and most peers fall, then breaks out.",
        "assets": "Single asset vs benchmark and 20+ peers",
        "data": ["1-minute prices", "benchmark", "20+ synchronized peers", "residual model"],
        "backtest": ["crypto-archive", "us-archive"],
        "paper": ["crypto-paper"],
    },
    {
        "id": "h02",
        "doc": HANDBOOK,
        "number": 2,
        "priority": True,
        "idea": "Three two-minute buying bursts that each keep at least 70% of their gain.",
        "assets": "Single asset",
        "data": ["aggressor-labelled trades", "quotes"],
        "backtest": ["crypto-archive"],
        "paper": ["crypto-paper"],
        "note": "US SIP bars carry no buyer/seller labels; equities need a separately "
        "registered tick-rule variant.",
    },
    {
        "id": "h03",
        "doc": HANDBOOK,
        "number": 3,
        "idea": "After a broad breakout, two pullbacks that are shallower, shorter and see "
        "less selling.",
        "assets": "Single asset with market breadth",
        "data": ["aggressor-labelled trades", "quotes", "peer breadth"],
        "backtest": ["crypto-archive"],
        "paper": ["crypto-paper"],
    },
    {
        "id": "h04",
        "doc": HANDBOOK,
        "number": 4,
        "idea": "Short a rebound that stalls in a prior high-volume area despite heavy buying.",
        "assets": "Shortable contract (perpetual)",
        "data": ["trade-price volume profile", "aggressor labels", "quotes", "short access"],
        "backtest": ["crypto-archive"],
        "paper": ["crypto-paper"],
        "note": "Perpetuals only: equity shorts need borrow data the project does not have.",
    },
    {
        "id": "h05",
        "doc": HANDBOOK,
        "number": 5,
        "idea": "Constituents improve on a residual basis before the index ETF responds.",
        "assets": "Index ETF plus its full constituent set",
        "data": ["complete constituent quotes", "historical index membership and weights"],
        "backtest": [],
        "paper": [],
        "blocked": "Needs synchronized quotes for a complete point-in-time constituent set "
        "(e.g. all S&P 500 members). The stored universe has 11 stocks; the free Alpaca "
        "stream allows 30 symbols. Implemented and unit-tested, waiting for data.",
    },
    {
        "id": "h06",
        "doc": HANDBOOK,
        "number": 6,
        "idea": "Three touches of resistance with less buying each time, then a real break.",
        "assets": "Single asset",
        "data": ["aggressor-labelled trades", "quotes", "seasonal volume"],
        "backtest": ["crypto-archive"],
        "paper": ["crypto-paper"],
    },
    {
        "id": "h07",
        "doc": HANDBOOK,
        "number": 7,
        "idea": "The traded centre (VWAP) rises while the range narrows, then expands upward.",
        "assets": "Single asset",
        "data": ["trade prices and quantities (VWAP)", "quotes"],
        "backtest": ["crypto-archive"],
        "paper": ["crypto-paper"],
        "note": "Excluded from the US book: SIP bars lack trade VWAP.",
    },
    {
        "id": "h08",
        "doc": HANDBOOK,
        "number": 8,
        "idea": "A related laggard catches up after its leader moves first.",
        "assets": "Predeclared leader/laggard pairs (multi-asset)",
        "data": ["synchronized quotes for both legs", "60 sessions for the stability screen"],
        "backtest": ["crypto-archive", "us-archive"],
        "paper": ["crypto-paper"],
        "note": "US data covers about 50 sessions; the 60-session stability screen means "
        "no US setups can arm yet.",
    },
    {
        "id": "h09",
        "doc": HANDBOOK,
        "number": 9,
        "priority": True,
        "idea": "Three comparable selling bursts that each do less damage and recover faster.",
        "assets": "Single asset",
        "data": ["aggressor-labelled trades", "quotes", "quoted spreads"],
        "backtest": [],
        "paper": ["crypto-paper"],
        "note": "Primary version needs quoted spreads, so it runs on the paper desk. The "
        "archive runs h09t, a separately registered variant without the spread rule.",
    },
    {
        "id": "h09t",
        "doc": HANDBOOK,
        "number": 9,
        "variant_of": "h09",
        "idea": "Strategy 9 without the spread-comparability rule (trade archives have no quotes).",
        "assets": "Single asset",
        "data": ["aggressor-labelled trades"],
        "backtest": ["crypto-archive"],
        "paper": [],
    },
    {
        "id": "h10",
        "doc": HANDBOOK,
        "number": 10,
        "idea": "A thin-session breakout that survives the main-session volume handoff.",
        "assets": "Single asset with a declared session handoff",
        "data": ["thin-session quotes and trades", "exchange calendar"],
        "backtest": ["crypto-archive"],
        "paper": ["crypto-paper"],
        "note": "Crypto uses the NYSE open as a registered handoff convention. Equities need "
        "pre-market bars; the stored SIP data is regular session only.",
    },
    {
        "id": "n01",
        "doc": NOTEBOOK,
        "title": "Missing Breakdown",
        "number": 1,
        "priority": True,
        "idea": "Stocks that repeatedly avoid declines a calibrated model expected.",
        "assets": "Cross-section vs market/sector",
        "data": ["intraday candles", "market and sector prices", "multi-session history"],
        "backtest": [],
        "paper": [],
        "blocked": "Model strategy (breakdown-probability model plus a return model) with "
        "1-5 session horizons; needs several years of history per name for calibration. "
        "Not implemented in this release.",
    },
    {
        "id": "n02",
        "doc": NOTEBOOK,
        "title": "Ordered Shock Prediction",
        "number": 2,
        "idea": "A moving before B predicts C beyond A and B separately.",
        "assets": "Three related instruments (multi-asset)",
        "data": ["synchronized prices across related stocks"],
        "backtest": [],
        "paper": [],
        "blocked": "Sparse interaction model over thousands of sequences; not implemented "
        "in this release.",
    },
    {
        "id": "n03",
        "doc": NOTEBOOK,
        "title": "Downside Separation With Upside Reconnection",
        "number": 3,
        "priority": True,
        "idea": "More participation in market rallies, less in market selloffs.",
        "assets": "Stock vs market (multi-asset)",
        "data": ["years of stock and benchmark returns"],
        "backtest": [],
        "paper": [],
        "blocked": "Tail-dependence estimates at 10% tails need years of daily data; the "
        "stored history is 2.5 months (US) and 9 months (crypto). Not implemented.",
    },
    {
        "id": "n04",
        "doc": NOTEBOOK,
        "title": "Directional Recovery Clocks",
        "number": 4,
        "priority": True,
        "idea": "Drops recover faster than rallies fade, measured in time.",
        "assets": "Single asset",
        "data": ["detailed intraday price paths"],
        "backtest": [],
        "paper": [],
        "blocked": "Censored-duration model; not implemented in this release.",
    },
    {
        "id": "n05",
        "doc": NOTEBOOK,
        "title": "Timeframe Disagreement",
        "number": 5,
        "idea": "Four 15-minute steps disagree with one direct 60-minute forecast.",
        "assets": "Single asset",
        "data": ["several resolutions of price/state data"],
        "backtest": [],
        "paper": [],
        "blocked": "Transition-operator model; not implemented.",
    },
    {
        "id": "n06",
        "doc": NOTEBOOK,
        "title": "Liquidity Replenishment Failure",
        "number": 6,
        "idea": "Sell-side liquidity refills unusually slowly.",
        "assets": "Single asset order book",
        "data": ["order-book updates"],
        "backtest": [],
        "paper": [],
        "blocked": "Needs full depth updates; only forward recording can supply them. "
        "Not implemented.",
    },
    {
        "id": "n07",
        "doc": NOTEBOOK,
        "title": "Overlapping Trading Programs",
        "number": 7,
        "idea": "Recurring buying programs overlap before net pressure appears.",
        "assets": "Single asset trade stream",
        "data": ["timestamped trades and quotes"],
        "backtest": [],
        "paper": [],
        "blocked": "Hawkes model on trade events; not implemented.",
    },
    {
        "id": "n08",
        "doc": NOTEBOOK,
        "title": "Upper-Tail Escape",
        "number": 8,
        "priority": True,
        "idea": "A modest rise unusually likely to become a much larger one within five sessions.",
        "assets": "Cross-section",
        "data": ["price paths", "context features", "large multi-year cross-section"],
        "backtest": [],
        "paper": [],
        "blocked": "Competing-risk classifier for +10%/-4% five-session outcomes; needs a "
        "broad multi-year cross-section with delisted names. Not implemented.",
    },
    {
        "id": "n09",
        "doc": NOTEBOOK,
        "title": "Reserve Exhaustion",
        "number": 9,
        "idea": "A level becomes more sensitive to buying across repeated visits.",
        "assets": "Single asset order book",
        "data": ["trades", "quotes", "displayed depth"],
        "backtest": [],
        "paper": [],
        "blocked": "Needs displayed depth history; not implemented.",
    },
    {
        "id": "n10",
        "doc": NOTEBOOK,
        "title": "Activity-Clock Dislocation",
        "number": 10,
        "idea": "Lead-lag measured on an activity clock instead of wall-clock time.",
        "assets": "Several instruments (multi-asset)",
        "data": ["synchronized trade or quote events"],
        "backtest": [],
        "paper": [],
        "blocked": "Activity-time sparse lag model; not implemented.",
    },
]


def catalog() -> list[dict[str, Any]]:
    output = []
    for entry in ENTRIES:
        implemented = entry["id"] in REGISTRY
        item = dict(entry)
        item["implemented"] = implemented
        if implemented:
            cls = REGISTRY[entry["id"]]
            item["title"] = cls.title
            item["direction"] = "short" if cls.direction < 0 else "long"
            item["time_exit_minutes"] = cls.time_exit_minutes
        if item.get("backtest") and item.get("paper"):
            item["mode"] = "backtest + paper"
        elif item.get("paper"):
            item["mode"] = "paper only"
        elif item.get("backtest"):
            item["mode"] = "backtest only"
        else:
            item["mode"] = "blocked"
        output.append(item)
    return output

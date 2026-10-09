"""Plain-language entry rules for every strategy, stage by stage, for the dashboard.

Each strategy is a sequence of stages. A stage lists what moves the setup forward
("when") and what ends it without a trade ("stop"). Then come the order and its
exits. The words follow the code in ``xasset.lab.strategies`` exactly; thresholds are
written in words (sigma means the coin's usual move over that many minutes, measured
on the previous 20 days at the same time of day). Mirror variants reuse the text with
directional words exchanged (falls/rises, high/low, buying/selling, long/short).
"""

from __future__ import annotations

import re
from typing import Any

COMMON_EXITS = [
    "Profit target: twice the distance from the fill to the stop.",
    "Stop: triggers on the executable side and fills at the next price, gaps included.",
    "If stop and target fall inside the same minute, the stop is assumed first.",
]

Stage = dict[str, Any]

STAGES: dict[str, list[Stage]] = {
    "h01": [
        {
            "state": "Armed: the market falls, the coin holds",
            "when": [
                "BTC fell over the last 10 minutes, and the fall is in its worst 10% for this time of day.",  # noqa: E501
                "At least 70% of the other coins fell over the same 10 minutes.",
                "The coin fell less than half of its usual 10-minute move.",
                "The coin is at least one usual move above what its normal sensitivity to BTC predicts.",  # noqa: E501
            ],
            "freeze": "The coin's 10-minute low K0 and BTC's 10-minute low KM.",
        },
        {
            "state": "Stabilized: the market stops making lows",
            "when": [
                "At least 3 minutes after arming, BTC is flat or up over 3 minutes and its last three lows are at or above KM.",  # noqa: E501
            ],
            "stop": [
                "The coin closes below K0.",
                "BTC trades below KM.",
                "30 minutes pass since arming.",
            ],
        },
        {
            "state": "Confirmed: a breakout",
            "when": [
                "At least 5 minutes after arming, a rising minute closes above the coin's high of the previous 5 minutes.",  # noqa: E501
            ],
            "stop": ["The expiry rules of the previous stage."],
        },
    ],
    "h02": [
        {
            "state": "Burst 1",
            "when": [
                "A fixed 2-minute block (counted from 00:00 UTC) has net buying in its top 10% for this time of day, at least 20% of its volume is net buying, and it rises at least half its usual 2-minute move.",  # noqa: E501
            ],
            "freeze": "The block's start and end prices.",
        },
        {
            "state": "Pause, then burst 2",
            "when": [
                "One to three quiet blocks follow, and at every minute the coin keeps at least 70% of the burst's gain.",  # noqa: E501
                "Then a second block passes the same buying rule.",
            ],
            "stop": [
                "The coin gives back more than 30% of the burst's gain.",
                "A fourth quiet block in a row.",
                "More than 20 minutes since burst 1.",
            ],
        },
        {
            "state": "Confirmed: burst 3 makes a new high",
            "when": [
                "After another valid pause, a third buying burst ends above every high since burst 1.",  # noqa: E501
            ],
            "stop": ["The third burst does not end above that high."],
        },
    ],
    "h03": [
        {
            "state": "Armed: a broad breakout",
            "when": [
                "The coin closes above its 60-minute high (level U) on a rising minute.",
                "BTC is up over the last 10 minutes.",
                "At least 60% of the other coins are up over the last 10 minutes.",
            ],
            "freeze": "The breakout level U.",
        },
        {
            "state": "Two pullbacks",
            "when": [
                "A close at least half a usual 5-minute move below the running high starts a pullback.",  # noqa: E501
                "It must climb back to within a tenth of that move of the high within 10 minutes.",
                "Depth, duration and selling are measured for each pullback.",
            ],
            "stop": [
                "A pullback does not recover within 10 minutes.",
                "A close more than half a usual 5-minute move below U.",
                "45 minutes pass since the breakout.",
            ],
        },
        {
            "state": "Confirmed: the second pullback is weaker",
            "when": [
                "Pullback 2 is shallower, shorter and sees less selling than pullback 1, and its recovery minute closes above the high of the previous 3 minutes.",  # noqa: E501
            ],
            "stop": ["Any of the three is not smaller, or the recovery minute does not break out."],  # noqa: E501
        },
    ],
    "h04": [
        {
            "state": "Armed: a sharp drop",
            "when": [
                "The coin falls at least two usual 10-minute moves in 10 minutes on volume in its top 10%.",  # noqa: E501
                "A heavily traded price band [A, B] sits in the upper 30% of the 2 hours before the drop, above the drop low.",  # noqa: E501
            ],
            "freeze": "The band [A, B], the pre-drop high and the drop low.",
        },
        {
            "state": "At the area: the rebound reaches the band",
            "when": [
                "Within 60 minutes, a close inside [A, B] that has recovered at least half the drop.",  # noqa: E501
            ],
            "stop": [
                "A close more than half a usual move above B.",
                "60 minutes pass since the drop.",
            ],
        },
        {
            "state": "Absorbed: heavy buying, no progress",
            "when": [
                "The next 10 minutes all close inside [A, B], buying is in its top 10%, net buying is at least 20% of volume, and price moves less than a quarter of a usual 10-minute move.",  # noqa: E501
            ],
            "stop": ["Any close outside the band, or buying not heavy, or price makes progress."],  # noqa: E501
            "freeze": "The high of those 10 minutes (Hc).",
        },
        {
            "state": "Confirmed: the rebound fails",
            "when": ["A close below the low of the previous 5 minutes."],
            "stop": ["The same 60-minute and band rules."],
        },
    ],
    "h06": [
        {
            "state": "Visit 1 at resistance",
            "when": [
                "The coin closes within a tenth of a usual 5-minute move below its 60-minute high (resistance U).",  # noqa: E501
            ],
            "freeze": "Resistance U and the buying effort of the last 5 minutes.",
        },
        {
            "state": "Retreat, then visits 2 and 3",
            "when": [
                "After each visit the coin retreats at least a quarter of a usual move below U, then returns to the band at least 5 minutes after the previous visit.",  # noqa: E501
            ],
            "stop": [
                "A breakout above U before three visits.",
                "A return to the band within 5 minutes of the last visit.",
                "A close more than two usual moves below U, or 45 minutes since visit 1.",
            ],
        },
        {
            "state": "Confirmed: less buying each time, then a breakout",
            "when": [
                "Buying effort shrank at each visit and the second retreat was shallower than the first; then a close more than one tick above U.",  # noqa: E501
            ],
            "stop": ["No breakout within 5 minutes of visit 3."],
        },
    ],
    "h07": [
        {
            "state": "Armed: a tightening range with a rising center",
            "when": [
                "Three fixed 10-minute blocks: each range narrower than the last, the third at most 70% of the first.",  # noqa: E501
                "The volume-weighted price rises block to block, by at least half a usual 10-minute move overall.",  # noqa: E501
                "The third block's volume-weighted price sits in the upper 40% of its range, and its volume is not in its bottom 25%.",  # noqa: E501
            ],
            "freeze": "The third block's high U and low K0.",
        },
        {
            "state": "Confirmed: a range-expanding breakout",
            "when": [
                "Within 10 minutes, a close more than one tick above U on a minute whose range is in its top 25%.",  # noqa: E501
            ],
            "stop": ["A close below K0.", "10 minutes pass."],
        },
    ],
    "h08": [
        {
            "state": "Armed: the leader jumps, the laggard has not",
            "when": [
                "A predeclared leader (BTC, ETH or SOL) jumps: its 5-minute rise is in its top 5%.",  # noqa: E501
                "The laggard is behind by at least one usual gap, given its lag model on the leader; the pair's relationship stayed positive in each of the last three 20-day windows.",  # noqa: E501
            ],
            "freeze": "The laggard's 5-minute low K0 and the leader's price before the jump.",
        },
        {
            "state": "Confirmed: the laggard starts to follow",
            "when": [
                "Within 10 minutes the laggard rises over 2 minutes and closes above its high of the previous 3 minutes, while the leader keeps at least 70% of its jump.",  # noqa: E501
            ],
            "stop": [
                "The leader gives back more than 30% of its jump.",
                "The gap closes before confirmation.",
                "The laggard trades below K0, or 10 minutes pass.",
            ],
        },
    ],
    "h09": [
        {
            "state": "Selling burst 1",
            "when": [
                "A fixed 2-minute block has net selling in its top 10%, at least 20% of its volume is net selling, and it falls by at least a quarter of a usual 2-minute move.",  # noqa: E501
            ],
            "freeze": "The price before the burst and the burst's low.",
        },
        {
            "state": "Recovery, then bursts 2 and 3",
            "when": [
                "Within 6 minutes a block (not another selling burst) ends back within a tenth of a usual move of the pre-burst price.",  # noqa: E501
                "The next burst comes within 10 minutes of the recovery, after at least one quiet block; all three bursts within 40 minutes.",  # noqa: E501
            ],
            "stop": [
                "No recovery within 6 minutes, or a new selling burst before recovery.",
                "No next burst within 10 minutes, or no quiet block in between.",
            ],
        },
        {
            "state": "Confirmed: each fall does less harm",
            "when": [
                "Selling and local volatility are comparable across the three bursts (largest at most 1.25 times the smallest), the size of the falls and the recovery times both shrink, and within 3 minutes of the last recovery a close is above the previous 3-minute high.",  # noqa: E501
            ],
            "stop": [
                "Not comparable, or the falls or recovery times not shrinking.",
                "Burst 3's low breaks, or price slips below the recovery level, before entry.",  # noqa: E501
            ],
        },
    ],
    "h10": [
        {
            "state": "Thin-session breakout before the US open",
            "when": [
                "U and L are the high and low from 120 to 30 minutes before the NYSE open; the first close more than one tick above U comes in the last 30 minutes before the open.",  # noqa: E501
                "Volume in that thin 2-hour window is less than its 20-day median.",
            ],
            "freeze": "The thin-window high U and low L.",
        },
        {
            "state": "Handoff: the open brings volume",
            "when": [
                "Volume in the first 5 minutes after the open is in its top 40%, and every close since the breakout stayed at or above U.",  # noqa: E501
            ],
            "stop": ["A close below U.", "Opening volume not in its top 40%."],
        },
        {
            "state": "Pullback, then confirmation",
            "when": [
                "Between 5 and 20 minutes after the open, the first minute whose low touches U (within a quarter of a usual move) while closing above U.",  # noqa: E501
                "A later minute closes above that minute's high.",
            ],
            "stop": ["A close below U.", "No confirmation 20 minutes after the open."],
        },
    ],
}
STAGES["h09t"] = STAGES["h09"]

ENTRIES: dict[str, dict[str, Any]] = {
    "h01": {"order": "Buy at the next minute's price", "stop": "one tick below K0", "time": 60},
    "h02": {"order": "Buy at the next minute's price", "stop": "one tick below the final pause low", "time": 60},  # noqa: E501
    "h03": {"order": "Buy at the next minute's price", "stop": "one tick below pullback 2's low", "time": 60},  # noqa: E501
    "h04": {"order": "Sell short at the next minute's price", "stop": "one tick above Hc", "time": 60},  # noqa: E501
    "h06": {"order": "Buy at the next minute's price", "stop": "one tick below the low between visits 2 and 3", "time": 60},  # noqa: E501
    "h07": {"order": "Buy at the next minute's price", "stop": "one tick below K0", "time": 60},
    "h08": {"order": "Buy the laggard at the next minute's price", "stop": "one tick below K0", "time": 20},  # noqa: E501
    "h09": {"order": "Buy at the next minute's price", "stop": "one tick below burst 3's low", "time": 60},  # noqa: E501
    "h09t": {"order": "Buy at the next minute's price", "stop": "one tick below burst 3's low", "time": 60},  # noqa: E501
    "h10": {"order": "Buy at the next minute's price", "stop": "one tick below the pullback minute's low", "time": 30},  # noqa: E501
}  # fmt: skip

FLIPS = {
    "fell": "rose", "rose": "fell", "falls": "rises", "rises": "falls", "fall": "rise",
    "rise": "fall", "falling": "rising", "rising": "falling", "drop": "rally",
    "rally": "drop", "down": "up", "up": "down", "buying": "selling", "selling": "buying",
    "buy": "sell", "sell": "buy", "buyer": "seller", "seller": "buyer", "high": "low",
    "low": "high", "highs": "lows", "lows": "highs", "above": "below", "below": "above",
    "breakout": "breakdown", "breakdown": "breakout", "resistance": "support",
    "support": "resistance", "upper": "lower", "lower": "upper", "worst": "best",
    "best": "worst", "long": "short", "short": "long", "jumps": "slumps", "jump": "slump",
    "gain": "loss", "gains": "losses", "pullback": "bounce", "pullbacks": "bounces",
    "rebound": "decline", "climb": "drop", "shallower": "shallower",
}  # fmt: skip


def flip(text: str) -> str:
    def swap(match: re.Match[str]) -> str:
        word = match.group(0)
        out = FLIPS.get(word.lower())
        if out is None:
            return word
        if word.isupper() and len(word) > 1:
            return out.upper()
        return out.capitalize() if word[:1].isupper() else out

    return re.sub(r"[A-Za-z]+", swap, text)


def flipped(value: Any) -> Any:
    if isinstance(value, str):
        return flip(value)
    if isinstance(value, list):
        return [flipped(v) for v in value]
    if isinstance(value, dict):
        return {k: (v if k == "time" else flipped(v)) for k, v in value.items()}
    return value


def explanation(strategy_id: str) -> dict[str, Any] | None:
    """Stages, order and exits for a strategy ID (mirror IDs end in "m")."""
    base = strategy_id[:-1] if strategy_id.endswith("m") else strategy_id
    if base not in STAGES:
        return None
    stages, entry = STAGES[base], dict(ENTRIES[base])
    output = {
        "stages": stages,
        "order": entry["order"],
        "stop": entry["stop"],
        "time_exit_minutes": entry["time"],
        "exits": COMMON_EXITS,
        "mirror": False,
    }
    if base != strategy_id:
        output = flipped(output)
        output["exits"] = COMMON_EXITS  # not directional
        output["mirror"] = True
        output["note"] = (
            f"Mirror of {base}: the identical rule applied to the inverted price 1/P with "
            "buyer- and seller-initiated volume exchanged, trading the opposite side. "
            "Not a handbook strategy; registered as a separate variant."
        )
    return output

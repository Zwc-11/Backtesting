"""Session tapes, causal features, frozen per-session models and calibrations.

Every quantity is computed from bars already completed and available. Online values
(scalar functions over the current session) and session-end arrays (vectorized over a
finished session for the reference history) use one definition each; a unit test
asserts they agree. Windows never span a session boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from xasset.lab.bars import MINUTE, Basis, FlowBar
from xasset.lab.calibration import Calibration
from xasset.lab.sessions import Session, Sessions
from xasset.lab.universe import Book, Pair, Universe

COVERAGE = 0.95  # minimum aggressor-labelled share of notional in flow windows
RETURN_HORIZONS = (1, 2, 3, 5, 10)
# Calibrated per-instrument features (session-end arrays).
FEATURES = (
    "R1",
    "R2",
    "R3",
    "R5",
    "R10",
    "N1",  # one-minute total notional
    "N10",  # rolling ten-minute notional
    "B10",  # rolling ten-minute buyer-initiated notional (coverage-checked)
    "RANGE1",  # one-minute log high/low range
    "Q2",  # two-minute block signed flow at block-end minutes (coverage-checked)
    "E10",  # ten-minute cumulative residual with the session's frozen model
)


@dataclass
class Tape:
    """One instrument's bars in the current session, indexed by minute of session.

    Numpy arrays feed the vectorized session-end features; Python lists with
    run lengths (consecutive valid minutes ending at each index) serve the scalar
    accessors strategies call every minute. Both are written by ``put``.
    """

    width: int
    bars: list[FlowBar | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.bars = [None] * self.width
        nan = np.full(self.width, np.nan)
        self.o, self.h, self.l, self.c = nan.copy(), nan.copy(), nan.copy(), nan.copy()
        self.lc = nan.copy()
        self.notional, self.volume = nan.copy(), nan.copy()
        self.buy, self.sell, self.uncl = nan.copy(), nan.copy(), nan.copy()
        self.spread = nan.copy()
        self.last = -1
        width = self.width
        self._c: list[float] = [math.nan] * width
        self._h: list[float] = [math.nan] * width
        self._l: list[float] = [math.nan] * width
        self._lc: list[float] = [math.nan] * width
        self._n: list[float] = [math.nan] * width
        self._b: list[float] = [math.nan] * width
        self._s: list[float] = [math.nan] * width
        self._price_run: list[int] = [0] * width  # consecutive minutes with prices
        self._bar_run: list[int] = [0] * width  # consecutive minutes with a bar
        self._flow_run: list[int] = [0] * width  # consecutive minutes with labelled flow
        # Derived values of completed minutes shared by strategies (reset each session).
        self.memo: dict[tuple[str, int], object] = {}

    def put(self, index: int, bar: FlowBar, basis: Basis) -> None:
        if not 0 <= index < self.width:
            raise ValueError("Bar outside the session width")
        if index <= self.last:
            raise ValueError("Bars of one instrument must arrive in increasing minute order")
        self.bars[index] = bar
        price = bar.price(basis)
        previous = index - 1
        contiguous = previous == self.last and previous >= 0
        if price is not None:
            self.o[index], self.h[index], self.l[index], self.c[index] = price
            log_close = math.log(price[3])
            self.lc[index] = log_close
            self._h[index], self._l[index], self._c[index] = price[1], price[2], price[3]
            self._lc[index] = log_close
            self._price_run[index] = (self._price_run[previous] if contiguous else 0) + 1
        self.notional[index] = bar.notional
        self.volume[index] = bar.volume
        self._n[index] = bar.notional
        self._bar_run[index] = (self._bar_run[previous] if contiguous else 0) + 1
        if bar.buy_notional is not None and bar.sell_notional is not None:
            self.buy[index], self.sell[index] = bar.buy_notional, bar.sell_notional
            self.uncl[index] = bar.unclassified_notional or 0.0
            self._b[index], self._s[index] = bar.buy_notional, bar.sell_notional
            self._flow_run[index] = (self._flow_run[previous] if contiguous else 0) + 1
        if bar.spread_rel is not None:
            self.spread[index] = bar.spread_rel
        self.last = index

    def _complete(self, runs: list[int], first: int, last: int) -> bool:
        return 0 <= first <= last < self.width and runs[last] >= last - first + 1

    # Scalar, causal accessors over completed minutes of this session.
    def close(self, i: int) -> float | None:
        if 0 <= i < self.width:
            value = self._c[i]
            return value if value == value else None
        return None

    def high(self, i: int) -> float | None:
        if 0 <= i < self.width:
            value = self._h[i]
            return value if value == value else None
        return None

    def low(self, i: int) -> float | None:
        if 0 <= i < self.width:
            value = self._l[i]
            return value if value == value else None
        return None

    def ret(self, i: int, h: int) -> float | None:
        """log(m_i / m_{i-h}); requires all h+1 closes i-h..i in this session."""
        if not self._complete(self._price_run, i - h, i):
            return None
        return self._lc[i] - self._lc[i - h]

    def sum_notional(self, first: int, last: int) -> float | None:
        if not self._complete(self._bar_run, first, last):
            return None
        return float(sum(self._n[first : last + 1]))

    def flow(self, first: int, last: int) -> tuple[float, float, float] | None:
        """(buy, sell, total) notional over minutes first..last, or None if coverage fails.

        Requires every minute observed and classified notional >= 95% of the total.
        Zero total notional leaves imbalance undefined, so it also returns None.
        """
        if not self._complete(self._flow_run, first, last):
            return None
        b = float(sum(self._b[first : last + 1]))
        s = float(sum(self._s[first : last + 1]))
        v = float(sum(self._n[first : last + 1]))
        if v <= 0 or b + s < COVERAGE * v:
            return None
        return b, s, v

    def imbalance(self, i: int, h: int) -> float | None:
        flow = self.flow(i - h + 1, i)
        return None if flow is None else (flow[0] - flow[1]) / flow[2]

    def max_high(self, first: int, last: int) -> float | None:
        if not self._complete(self._price_run, first, last):
            return None
        return max(self._h[first : last + 1])

    def min_low(self, first: int, last: int) -> float | None:
        if not self._complete(self._price_run, first, last):
            return None
        return min(self._l[first : last + 1])


def rolling_valid_sum(values: np.ndarray, window: int) -> np.ndarray:
    """Sum over the trailing window ending at each index; NaN unless all finite."""
    out = np.full(values.shape, np.nan)
    finite = np.isfinite(values)
    filled = np.where(finite, values, 0.0)
    csum = np.concatenate([[0.0], np.cumsum(filled)])
    ccount = np.concatenate([[0], np.cumsum(finite)])
    if window <= values.size:
        idx = np.arange(window - 1, values.size)
        sums = csum[idx + 1] - csum[idx + 1 - window]
        counts = ccount[idx + 1] - ccount[idx + 1 - window]
        out[idx] = np.where(counts == window, sums, np.nan)
    return out


def returns_array(lc: np.ndarray, h: int) -> np.ndarray:
    out = np.full(lc.shape, np.nan)
    if h < lc.size:
        complete = rolling_valid_sum(np.where(np.isfinite(lc), 0.0, np.nan), h + 1)
        diff = lc[h:] - lc[:-h]
        out[h:] = np.where(np.isfinite(complete[h:]), diff, np.nan)
    return out


def tape_features(tape: Tape) -> dict[str, np.ndarray]:
    """Session-end feature arrays; each matches the online definition at every index."""
    out = {f"R{h}": returns_array(tape.lc, h) for h in RETURN_HORIZONS}
    out["N1"] = tape.notional.copy()
    out["N10"] = rolling_valid_sum(tape.notional, 10)
    buy10 = rolling_valid_sum(tape.buy, 10)
    sell10 = rolling_valid_sum(tape.sell, 10)
    total10 = out["N10"]
    with np.errstate(invalid="ignore"):
        covered = (total10 > 0) & (buy10 + sell10 >= COVERAGE * total10)
    out["B10"] = np.where(covered, buy10, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["RANGE1"] = np.log(tape.h / tape.l)
    q2 = np.full(tape.width, np.nan)
    buy2, sell2, total2 = (rolling_valid_sum(x, 2) for x in (tape.buy, tape.sell, tape.notional))
    ends = np.arange(1, tape.width, 2)  # blocks [2k, 2k+1] anchored to the session open
    with np.errstate(invalid="ignore"):
        ok = (total2[ends] > 0) & (buy2[ends] + sell2[ends] >= COVERAGE * total2[ends])
    q2[ends] = np.where(ok, buy2[ends] - sell2[ends], np.nan)
    out["Q2"] = q2
    return out


def block_flow(tape: Tape, end: int) -> float | None:
    """Online two-minute block signed flow at a block-end minute (odd index)."""
    if end % 2 != 1:
        return None
    flow = tape.flow(end - 1, end)
    return None if flow is None else flow[0] - flow[1]


@dataclass(frozen=True)
class LinearModel:
    intercept: float
    coefficients: tuple[float, ...]
    factors: tuple[str, ...]
    rows: int


@dataclass(frozen=True)
class LagModel:
    """Distributed-lag model r_i,u = alpha + sum_k beta_k r_L,u-k (k = 0..3)."""

    leader: str
    laggard: str
    intercept: float
    betas: tuple[float, ...]
    stable: bool
    rows: int

    @property
    def beta_sum(self) -> float:
        return float(sum(self.betas))


def ols(y: np.ndarray, x: np.ndarray, minimum: int) -> tuple[float, tuple[float, ...], int] | None:
    valid = np.isfinite(y) & np.isfinite(x).all(axis=1)
    rows = int(valid.sum())
    if rows < max(minimum, x.shape[1] + 2):
        return None
    design = np.column_stack([np.ones(rows), x[valid]])
    solution, *_ = np.linalg.lstsq(design, y[valid], rcond=None)
    if not np.isfinite(solution).all():
        return None
    return float(solution[0]), tuple(float(v) for v in solution[1:]), rows


def lagged(series: np.ndarray, k: int) -> np.ndarray:
    out = np.full(series.shape, np.nan)
    if k == 0:
        return series.copy()
    out[k:] = series[:-k]
    return out


class Market:
    """Shared causal state for one book: sessions, tapes, calibrations and models."""

    def __init__(self, universe: Universe, book: Book, basis: Basis, mirrored: bool = False):
        self.universe = universe
        self.book = book
        self.basis = basis
        # A mirrored market holds the bars of the price series 1/P with buyer- and
        # seller-initiated flow exchanged (see xasset.lab.mirror); ticks are relative.
        self.mirrored = mirrored
        self.members: frozenset[str] | None = None
        self._peer_cache: dict[tuple[int, int], dict[str, float]] = {}
        self._last_close: dict[str, float] = {}
        self._previous_close: dict[str, float] = {}  # last close of the previous session
        self._ticks = {item.id: item.tick for item in universe.instruments}
        self.sessions = Sessions(universe.calendar)
        self.width = self.sessions.max_minutes
        self.symbols = [item.id for item in universe.instruments]
        self.tapes = {symbol: Tape(self.width) for symbol in self.symbols}
        lag_sessions = 3 * book.calibration_sessions
        self.cal: dict[tuple[str, str], Calibration] = {}
        for symbol in self.symbols:
            for name in FEATURES:
                self.cal[(symbol, name)] = Calibration(
                    self.width,
                    book.calibration_sessions,
                    book.calibration_half_window,
                    book.minimum_reference,
                    keep=lag_sessions if name == "R1" else None,
                )
        self.pairs: list[tuple[str, str]] = [
            (pair.leader, laggard) for pair in universe.pairs for laggard in pair.laggards
        ]
        for leader, laggard in self.pairs:
            self.cal[(f"{leader}>{laggard}", "GAP5")] = Calibration(
                self.width,
                book.calibration_sessions,
                book.calibration_half_window,
                book.minimum_reference,
            )
        self.session: Session | None = None
        self.minute = -1
        self.end: datetime | None = None
        self.residual: dict[str, LinearModel | None] = {}
        self.lag: dict[tuple[str, str], LagModel | None] = {}
        self.sessions_seen = 0
        self.session_listeners: list[object] = []

    # --- time ---------------------------------------------------------------------
    def advance(self, end: datetime) -> bool:
        """Move to the minute ending at ``end``. False when it lies outside any session."""
        located = self.sessions.locate(end - MINUTE)
        if located is None:
            return False
        session, minute = located
        if self.session is None or session.key != self.session.key:
            self._roll(session)
        if minute < self.minute:
            raise ValueError("Bars must arrive in chronological order")
        if minute != self.minute or end != self.end:
            self._peer_cache = {}
        self.minute, self.end = minute, end
        return True

    def _roll(self, session: Session) -> None:
        if self.session is not None:
            self._finish_session()
        self._previous_close = dict(self._last_close)
        for calibration in self.cal.values():
            calibration.begin(session.key)
        for tape in self.tapes.values():
            tape.reset()
        self.session = session
        self.minute = -1
        self.sessions_seen += 1
        self.members = self.universe.members(f"{session.open:%Y-%m}")
        self._peer_cache = {}
        self._fit_models()

    def is_member(self, symbol: str) -> bool:
        return self.members is None or symbol in self.members

    def _finish_session(self) -> None:
        for symbol, tape in self.tapes.items():
            arrays = tape_features(tape)
            model = self.residual.get(symbol)
            arrays["E10"] = (
                rolling_valid_sum(self._residual_array(symbol, model), 10)
                if model is not None
                else np.full(self.width, np.nan)
            )
            for name, values in arrays.items():
                self.cal[(symbol, name)].record_array(values)
        for leader, laggard in self.pairs:
            lag = self.lag.get((leader, laggard))
            gaps = np.full(self.width, np.nan)
            if lag is not None:
                r_leader = returns_array(self.tapes[leader].lc, 5)
                r_laggard = returns_array(self.tapes[laggard].lc, 5)
                gaps = lag.beta_sum * r_leader - r_laggard
            self.cal[(f"{leader}>{laggard}", "GAP5")].record_array(gaps)

    def add(self, bar: FlowBar) -> None:
        if self.session is None or self.end is None or bar.end != self.end:
            raise ValueError("Advance the market to a bar's minute before adding it")
        if bar.symbol in self.tapes:
            tape = self.tapes[bar.symbol]
            tape.put(self.minute, bar, self.basis)
            close = tape.close(self.minute)
            if close is not None:
                self._last_close[bar.symbol] = close

    # --- models -------------------------------------------------------------------
    def factors(self, symbol: str) -> tuple[str, ...]:
        item = self.universe.get(symbol)
        names = []
        if symbol != self.universe.benchmark:
            names.append(self.universe.benchmark)
        if item.sector and item.sector not in (symbol, *names):
            names.append(item.sector)
        return tuple(names)

    def _history(self, symbol: str, count: int, offset: int = 0) -> np.ndarray | None:
        items = list(self.cal[(symbol, "R1")].history)
        if len(items) < count + offset:
            return None
        chosen = items[len(items) - offset - count : len(items) - offset]
        return np.concatenate(chosen)

    def _fit_models(self) -> None:
        sessions = self.book.calibration_sessions
        minimum = self.book.minimum_reference
        self.residual = {}
        for symbol in self.symbols:
            names = self.factors(symbol)
            y = self._history(symbol, sessions)
            xs = [self._history(name, sessions) for name in names]
            if not names or y is None or any(x is None for x in xs):
                self.residual[symbol] = None
                continue
            fitted = ols(y, np.column_stack([x for x in xs if x is not None]), minimum)
            self.residual[symbol] = (
                None if fitted is None else LinearModel(fitted[0], fitted[1], names, fitted[2])
            )
        self.lag = {}
        for leader, laggard in self.pairs:
            windows: list[tuple[float, tuple[float, ...], int] | None] = []
            for offset in (0, sessions, 2 * sessions):
                y_parts = list(self.cal[(laggard, "R1")].history)
                x_parts = list(self.cal[(leader, "R1")].history)
                if len(y_parts) < offset + sessions or len(x_parts) < offset + sessions:
                    windows.append(None)
                    continue
                stop = len(y_parts) - offset
                y_sessions = y_parts[stop - sessions : stop]
                x_sessions = x_parts[stop - sessions : stop]
                # Lags are taken within each session; never across a session boundary.
                y = np.concatenate(y_sessions)
                x = np.vstack(
                    [np.concatenate([lagged(s, k) for s in x_sessions]) for k in range(4)]
                ).T
                windows.append(ols(y, x, minimum))
            latest = windows[0]
            if latest is None:
                self.lag[(leader, laggard)] = None
                continue
            stable = all(w is not None and sum(w[1]) > 0 for w in windows)
            self.lag[(leader, laggard)] = LagModel(
                leader, laggard, latest[0], latest[1], stable, latest[2]
            )

    def _residual_array(self, symbol: str, model: LinearModel) -> np.ndarray:
        y = returns_array(self.tapes[symbol].lc, 1)
        fitted = np.full(self.width, model.intercept)
        for name, beta in zip(model.factors, model.coefficients, strict=True):
            fitted = fitted + beta * returns_array(self.tapes[name].lc, 1)
        residual: np.ndarray = y - fitted
        return residual

    # --- online features at the current minute -----------------------------------
    def residual_sum(self, symbol: str, i: int, h: int) -> float | None:
        """E_i(h): cumulative residual over minutes i-h+1..i with the frozen model."""
        model = self.residual.get(symbol)
        if model is None or i - h + 1 < 1:
            return None
        total = 0.0
        tape = self.tapes[symbol]
        for u in range(i - h + 1, i + 1):
            r = tape.ret(u, 1)
            if r is None:
                return None
            fitted = model.intercept
            for name, beta in zip(model.factors, model.coefficients, strict=True):
                f = self.tapes[name].ret(u, 1)
                if f is None:
                    return None
                fitted += beta * f
            total += r - fitted
        return total

    def gap(self, leader: str, laggard: str, i: int) -> float | None:
        model = self.lag.get((leader, laggard))
        if model is None:
            return None
        rl, ri = self.tapes[leader].ret(i, 5), self.tapes[laggard].ret(i, 5)
        if rl is None or ri is None:
            return None
        return model.beta_sum * rl - ri

    def scale(self, symbol: str, name: str, i: int) -> float | None:
        return self.cal[(symbol, name)].scale(i)

    def sigma(self, symbol: str, h: int, i: int, price: float) -> float | None:
        """Robust h-minute log-return scale with the one-tick floor at ``price``."""
        value = self.cal[(symbol, f"R{h}")].scale(i)
        if value is None or price <= 0:
            return None
        return max(value, self.relative_tick(symbol, price))

    def relative_tick(self, symbol: str, price: float) -> float:
        """One tick as a fraction of ``price`` (a price on this market's own scale).

        In a mirrored market ``price`` is 1/P, so the real tick over P is tick * price.
        """
        tick = self._ticks[symbol]
        return tick * price if self.mirrored else tick / price

    def tick(self, symbol: str) -> float:
        """One tick in this market's price units at the latest observed price."""
        tick = self._ticks[symbol]
        if not self.mirrored:
            return tick
        price = self._last_close.get(symbol)
        if price is None:
            raise ValueError(f"No observed price for {symbol} to express its tick")
        # d(1/P) = -dP / P^2, so one real tick spans tick * (1/P)^2 on the mirrored scale.
        return tick * price * price

    def quantile(self, symbol: str, name: str, i: int, p: float) -> float | None:
        return self.cal[(symbol, name)].quantile(i, p)

    def expected_notional(self, symbol: str, i: int) -> float | None:
        return self.cal[(symbol, "N1")].median(min(max(i, 0), self.width - 1))

    def peers(self, symbol: str) -> list[str]:
        return [
            item.id
            for item in self.universe.instruments
            if item.breadth_member
            and item.id not in (symbol, self.universe.benchmark)
            and self.is_member(item.id)
        ]

    def _peer_returns(self, i: int, h: int) -> dict[str, float]:
        """Valid h-minute returns of every eligible breadth member at minute i (cached)."""
        key = (i, h)
        cached = self._peer_cache.get(key)
        if cached is None:
            cached = {}
            for item in self.universe.instruments:
                if (
                    item.breadth_member
                    and item.id != self.universe.benchmark
                    and self.is_member(item.id)
                ):
                    value = self.tapes[item.id].ret(i, h)
                    if value is not None:
                        cached[item.id] = value
            self._peer_cache[key] = cached
        return cached

    def breadth(self, symbol: str, i: int, h: int, sign: int) -> tuple[float, int] | None:
        """Share of eligible peers whose h-minute return has the given sign, and count.

        Peers exclude ``symbol`` and the benchmark and must be members at the time.
        """
        returns = self._peer_returns(i, h)
        count = len(returns) - (1 if symbol in returns else 0)
        if count < self.universe.minimum_peers:
            return None
        if sign > 0:
            hits = sum(1 for s, v in returns.items() if v > 0 and s != symbol)
        else:
            hits = sum(1 for s, v in returns.items() if v < 0 and s != symbol)
        return hits / count, count

    def context(self, i: int) -> dict[str, float | None]:
        """The wider market at minute i: benchmark trend, its volatility regime, breadth.

        ``day`` is the benchmark's log return from the previous session's last close;
        ``vol_ratio`` is its realized one-minute volatility over the last 60 minutes over
        its prior-session scale (above 1 means a more volatile market than usual).
        """
        benchmark = self.tapes[self.universe.benchmark]
        out: dict[str, float | None] = {
            "market_60m": benchmark.ret(i, 60),
            "market_240m": benchmark.ret(i, 240),
        }
        close, previous = benchmark.close(i), self._previous_close.get(self.universe.benchmark)
        out["market_day"] = math.log(close / previous) if close and previous else None
        returns = [benchmark.ret(u, 1) for u in range(i - 59, i + 1)]
        valid = [r for r in returns if r is not None]
        scale = self.cal[(self.universe.benchmark, "R1")].scale(i)
        out["market_vol_ratio"] = (
            math.sqrt(sum(r * r for r in valid) / len(valid)) / scale
            if len(valid) >= 45 and scale
            else None
        )
        breadth = self.breadth(self.universe.benchmark, i, 60, 1)
        out["breadth_up_60m"] = None if breadth is None else breadth[0]
        return out


def pair_lookup(pairs: list[Pair]) -> dict[str, list[str]]:
    return {pair.leader: list(pair.laggards) for pair in pairs}

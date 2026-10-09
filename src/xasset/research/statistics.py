"""Daily-return DSR approximation and CSCV probability of backtest overfitting.

References: Bailey & López de Prado (2014), The Deflated Sharpe Ratio;
Bailey et al. (2017), The Probability of Backtest Overfitting. Serial dependence
is not corrected here; these diagnostics are not sufficient acceptance evidence.
"""

import itertools
import math
import statistics


def sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    deviation = statistics.stdev(returns)
    return statistics.mean(returns) / deviation if deviation > 1e-15 else None


def deflated_sharpe(
    returns: list[float], candidates: list[list[float]], trial_count: int
) -> float | None:
    observed = sharpe(returns)
    scores = [score for series in candidates if (score := sharpe(series)) is not None]
    if observed is None or len(returns) < 4 or trial_count < 1:
        return None
    benchmark = 0.0
    if trial_count > 1:
        if len(scores) < 2 or statistics.stdev(scores) <= 1e-15:
            return None  # Unknown search dispersion must not become a zero penalty.
        normal = statistics.NormalDist()
        gamma = 0.5772156649015329
        benchmark = statistics.stdev(scores) * (
            (1 - gamma) * normal.inv_cdf(1 - 1 / trial_count)
            + gamma * normal.inv_cdf(1 - 1 / (trial_count * math.e))
        )
    mean = statistics.mean(returns)
    m2 = statistics.mean([(value - mean) ** 2 for value in returns])
    skew = statistics.mean([(value - mean) ** 3 for value in returns]) / m2**1.5
    kurtosis = statistics.mean([(value - mean) ** 4 for value in returns]) / m2**2
    variance = 1 - skew * observed + (kurtosis - 1) * observed**2 / 4
    if variance <= 0:
        return None
    z = (observed - benchmark) * math.sqrt(len(returns) - 1) / math.sqrt(variance)
    return statistics.NormalDist().cdf(z)


def probability_overfit(candidates: list[list[float]], blocks: int = 8) -> float | None:
    if len(candidates) < 2 or blocks < 4 or blocks % 2:
        return None
    length = len(candidates[0])
    if length < blocks * 2 or any(len(series) != length for series in candidates):
        return None
    partitions = [
        list(range(i * length // blocks, (i + 1) * length // blocks)) for i in range(blocks)
    ]
    failures = 0
    combinations = list(itertools.combinations(range(blocks), blocks // 2))
    for chosen in combinations:
        train = [j for i in chosen for j in partitions[i]]
        test = [j for i in range(blocks) if i not in chosen for j in partitions[i]]
        train_scores = [sharpe([series[j] for j in train]) for series in candidates]
        test_scores = [sharpe([series[j] for j in test]) for series in candidates]
        if any(score is None for score in train_scores + test_scores):
            return None  # Never drop a failed/constant candidate from the penalty.
        ins = [float(score) for score in train_scores if score is not None]
        outs = [float(score) for score in test_scores if score is not None]
        winner = max(range(len(ins)), key=lambda i: (ins[i], -i))
        lower = sum(value < outs[winner] for value in outs)
        tied = sum(value == outs[winner] for value in outs)
        rank = lower + (tied + 1) / 2
        failures += rank / (len(outs) + 1) <= 0.5
    return failures / len(combinations)

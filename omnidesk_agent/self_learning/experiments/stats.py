"""Small exact and normal-approximation tests used by the learning gates.

No scipy: the runtime image stays small. Functions work in log space so that
exact tests stay accurate for thousands of samples.
"""
from __future__ import annotations

import math


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def binomial_sf(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    terms = [_log_comb(n, i) + i * log_p + (n - i) * log_q for i in range(k, n + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def mcnemar_one_sided(wins: int, losses: int) -> float:
    """Exact one-sided McNemar p-value that wins exceed losses (paired outcomes)."""
    return binomial_sf(wins, wins + losses, 0.5)


def fisher_one_sided_greater(x_t: int, n_t: int, x_c: int, n_c: int) -> float:
    """Exact one-sided Fisher p-value that the treatment rate exceeds the control rate."""
    total = x_t + x_c
    n = n_t + n_c
    if n_t <= 0 or n_c <= 0 or total == 0:
        return 1.0
    lo = max(x_t, total - n_c)
    hi = min(total, n_t)
    if lo > hi:
        return 0.0
    log_den = _log_comb(n, total)
    terms = [_log_comb(n_t, i) + _log_comb(n_c, total - i) - log_den for i in range(lo, hi + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def normal_quantile(p: float) -> float:
    """Inverse standard normal CDF (Acklam's approximation, |error| < 1.2e-9)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -normal_quantile(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def wilson_bounds(x: int, n: int, z: float) -> tuple[float, float]:
    """Wilson score interval for a proportion; stays inside [0, 1] at 0 and n."""
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def diff_bounds(x_t: int, n_t: int, x_c: int, n_c: int, alpha: float) -> tuple[float, float]:
    """One-sided (1 - alpha) lower and upper bounds for p_t - p_c (Newcombe hybrid score).

    Unlike the Wald interval it does not collapse when a rate is 0 or 1, so
    100 successes out of 100 are not mistaken for certainty.
    """
    if n_t <= 0 or n_c <= 0:
        return -1.0, 1.0
    z = normal_quantile(1 - alpha)
    p_t, p_c = x_t / n_t, x_c / n_c
    lo_t, hi_t = wilson_bounds(x_t, n_t, z)
    lo_c, hi_c = wilson_bounds(x_c, n_c, z)
    d = p_t - p_c
    lower = d - math.sqrt((p_t - lo_t) ** 2 + (hi_c - p_c) ** 2)
    upper = d + math.sqrt((hi_t - p_t) ** 2 + (p_c - lo_c) ** 2)
    return lower, upper

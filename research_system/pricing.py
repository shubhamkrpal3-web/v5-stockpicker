"""European option pricing on futures (Black-76) and implied volatility.

NSE stock and index options are European and cash-settled. Pricing off the
same-expiry futures price removes the need to model dividends separately.
Pure standard library so it runs anywhere.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float      # per 1.00 change in vol (divide by 100 for per vol point)
    theta: float     # per year (divide by 365 for per calendar day)


def black76(F: float, K: float, T: float, r: float, sigma: float, call: bool) -> Greeks:
    """Price and Greeks. F forward/futures, T years, r continuous rate, sigma vol."""
    if T <= 0 or sigma <= 0:
        intrinsic = max(F - K, 0.0) if call else max(K - F, 0.0)
        df = math.exp(-r * max(T, 0.0))
        delta = (1.0 if F > K else 0.0) if call else (-1.0 if F < K else 0.0)
        return Greeks(df * intrinsic, df * delta, 0.0, 0.0, 0.0)
    df = math.exp(-r * T)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    if call:
        price = df * (F * _ncdf(d1) - K * _ncdf(d2))
        delta = df * _ncdf(d1)
    else:
        price = df * (K * _ncdf(-d2) - F * _ncdf(-d1))
        delta = -df * _ncdf(-d1)
    gamma = df * _npdf(d1) / (F * sq)
    vega = df * F * _npdf(d1) * math.sqrt(T)
    theta = -df * F * _npdf(d1) * sigma / (2 * math.sqrt(T)) + r * price
    return Greeks(price, delta, gamma, vega, theta)


def implied_vol(price: float, F: float, K: float, T: float, r: float, call: bool,
                lo: float = 0.01, hi: float = 3.0, tol: float = 1e-6) -> float | None:
    """Bisection IV. None when the price is outside no-arbitrage bounds."""
    if price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    df = math.exp(-r * T)
    intrinsic = df * (max(F - K, 0.0) if call else max(K - F, 0.0))
    upper = df * (F if call else K)
    if price < intrinsic - 1e-9 or price >= upper:
        return None
    f_lo = black76(F, K, T, r, lo, call).price - price
    f_hi = black76(F, K, T, r, hi, call).price - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        v = black76(F, K, T, r, mid, call).price - price
        if abs(v) < tol:
            return mid
        if v > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)

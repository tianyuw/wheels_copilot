from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_put_delta(
    stock_price: float,
    strike: float,
    dte: int,
    implied_volatility: float,
    risk_free_rate: float = 0.04,
) -> float | None:
    if stock_price <= 0 or strike <= 0 or dte <= 0 or implied_volatility <= 0:
        return None
    t = dte / 365.0
    sigma = implied_volatility
    d1 = (
        math.log(stock_price / strike)
        + (risk_free_rate + 0.5 * sigma * sigma) * t
    ) / (sigma * math.sqrt(t))
    return norm_cdf(d1) - 1.0

"""Read prices from Kalshi market data as probabilities between 0 and 1.

Kalshi has used two formats: integer cents ("yes_bid": 42) and dollar strings
("yes_bid_dollars": "0.4200"). Both are handled; a zero or missing price is None.
"""

import math


def _price(market: dict, name: str) -> float | None:
    dollars = market.get(f"{name}_dollars")
    if dollars not in (None, ""):
        value = float(dollars)
    elif market.get(name) not in (None, ""):
        value = float(market[name]) / 100
    else:
        return None
    return value if 0 < value < 1 else None


def quotes(market: dict) -> dict[str, float | None]:
    """Best bid/ask for each side. A missing ask is implied by the other side's bid."""
    q = {k: _price(market, k) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")}
    if q["yes_ask"] is None and q["no_bid"] is not None:
        q["yes_ask"] = round(1 - q["no_bid"], 4)
    if q["no_ask"] is None and q["yes_bid"] is not None:
        q["no_ask"] = round(1 - q["yes_bid"], 4)
    return q


def settled_result(market: dict) -> str | None:
    result = (market.get("result") or "").lower()
    return result if result in {"yes", "no"} else None


def implied_yes_prob(market: dict) -> float | None:
    """Market's own estimate of P(yes): settled result, else bid/ask midpoint, else last price."""
    result = settled_result(market)
    if result:
        return 1.0 if result == "yes" else 0.0
    q = quotes(market)
    if q["yes_bid"] is not None and q["yes_ask"] is not None:
        return (q["yes_bid"] + q["yes_ask"]) / 2
    return _price(market, "last_price")


def kalshi_fee(price: float, contracts: int = 1, rate: float = 0.07) -> float:
    """Kalshi's trading fee: rate * contracts * P * (1 - P), rounded up to the cent."""
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100

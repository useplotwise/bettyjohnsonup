"""Price multi-leg combos from their individual legs.

A combo pays $1 only if every leg lands on its chosen side. If the legs are
roughly independent, its fair probability is the product of each leg's
probability. When the combo's ask is below that (after fees), it's a candidate.
Legs in the same game are often correlated, so treat results as estimates.
"""

from dataclasses import dataclass, field
from typing import Callable

from ..pricing import implied_yes_prob, kalshi_fee, quotes


@dataclass
class ComboEvaluation:
    ticker: str
    legs: list[tuple[str, str]]
    fair_prob: float | None
    ask: float | None
    bid: float | None
    edge: float | None  # expected profit per $1 staked, after fees
    legs_settled: int = 0
    missing_legs: list[str] = field(default_factory=list)

    @property
    def payout_multiple(self) -> float | None:
        return None if not self.ask else 1 / self.ask


def combo_legs(market: dict) -> list[tuple[str, str]]:
    """Return [(leg_market_ticker, side)] for a combo market."""
    selected = market.get("mve_selected_legs")
    if isinstance(selected, list) and selected and isinstance(selected[0], dict):
        return [
            (leg.get("market_ticker"), (leg.get("side") or "yes").lower())
            for leg in selected
            if leg.get("market_ticker")
        ]
    strike = market.get("custom_strike") or {}
    tickers = [t for t in str(strike.get("Associated Markets", "")).split(",") if t]
    sides = [s.strip().lower() for s in str(strike.get("Associated Market Sides", "")).split(",")]
    return [(t.strip(), sides[i] if i < len(sides) else "yes") for i, t in enumerate(tickers)]


def evaluate_combo(market: dict, get_leg: Callable[[str], dict | None]) -> ComboEvaluation:
    legs = combo_legs(market)
    q = quotes(market)
    fair, settled, missing = 1.0, 0, []
    for ticker, side in legs:
        leg = get_leg(ticker)
        p_yes = implied_yes_prob(leg) if leg else None
        if p_yes is None:
            missing.append(ticker)
            continue
        if p_yes in (0.0, 1.0):
            settled += 1
        fair *= p_yes if side == "yes" else 1 - p_yes
    fair_prob = None if missing or not legs else fair
    ask = q["yes_ask"]
    edge = None
    if fair_prob is not None and ask:
        edge = (fair_prob - ask - kalshi_fee(ask)) / ask
    return ComboEvaluation(
        ticker=market["ticker"],
        legs=legs,
        fair_prob=fair_prob,
        ask=ask,
        bid=q["yes_bid"],
        edge=edge,
        legs_settled=settled,
        missing_legs=missing,
    )

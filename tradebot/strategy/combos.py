"""Price multi-leg combos from their individual legs.

A combo pays $1 only if every leg lands on its chosen side. If the legs are
roughly independent, its fair probability is the product of each leg's
probability. When the combo's ask is below that (after fees), it's a candidate.
Legs in the same game are often correlated, so treat results as estimates. For
legs from the same game the product isn't trusted: the combo's chance is
instead the cautious lower bound max(0, sum of chances - (legs - 1)), which is
never above the product, so the bot doesn't claim an edge it can't back up.
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
    same_game_legs: int = 0  # legs that share a game with another leg

    @property
    def payout_multiple(self) -> float | None:
        return None if not self.ask else 1 / self.ask


def edge_at(fair_prob: float, ask: float) -> float:
    """Expected profit per $1 staked when buying YES at `ask`, after Kalshi's fee."""
    return (fair_prob - ask - kalshi_fee(ask)) / ask


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


def evaluate_combo(
    market: dict,
    get_leg: Callable[[str], dict | None],
    leg_prob: Callable[[dict], float | None] = implied_yes_prob,
) -> ComboEvaluation:
    """Fair chance of a combo. `leg_prob` gives each leg's YES chance (Kalshi's price by default)."""
    legs = combo_legs(market)
    q = quotes(market)
    fair, settled, missing = 1.0, 0, []
    games: dict[str, list[float]] = {}
    for ticker, side in legs:
        leg = get_leg(ticker)
        p_yes = leg_prob(leg) if leg else None
        if p_yes is None:
            missing.append(ticker)
            continue
        if p_yes in (0.0, 1.0):
            settled += 1
        p = p_yes if side == "yes" else 1 - p_yes
        game = leg.get("event_ticker")
        if game:
            games.setdefault(game, []).append(p)
        else:
            fair *= p
    same_game = 0
    for ps in games.values():
        if len(ps) == 1:
            fair *= ps[0]
        else:
            same_game += len(ps)
            fair *= max(0.0, sum(ps) - (len(ps) - 1))
    fair_prob = None if missing or not legs else fair
    ask = q["yes_ask"]
    edge = edge_at(fair_prob, ask) if fair_prob is not None and ask else None
    return ComboEvaluation(
        ticker=market["ticker"],
        legs=legs,
        fair_prob=fair_prob,
        ask=ask,
        bid=q["yes_bid"],
        edge=edge,
        legs_settled=settled,
        missing_legs=missing,
        same_game_legs=same_game,
    )

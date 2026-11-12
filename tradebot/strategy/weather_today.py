"""Pretend bets on today's temperature markets that live readings have already settled.

If a city's thermometer has already read 78F today, every "high of 75 or
below" market is lost for YES, and "high of 76 or above" is won, yet Kalshi's
price can lag behind for a while. This step looks for those markets (see
observations.decided_side) and records a pretend bet on the settled side
when the price still leaves an edge after Kalshi's fee.

- Its own group (paper kind "weather_today"), so it is scored and self-tuned
  apart from the forecast-based weather bets.
- The win chance is treated as CERTAIN_PROB, not 100%: a faulty reading or a
  revised report can still happen.
- At most one bet per city-day and market, ever: several settled markets on the
  same day all rest on the same readings, so betting them all is one bet many times.
- Stakes stay within the standard cap (no high-confidence exception) until the
  group has a track record. Pretend bets only; nothing here places a real order.
"""

import logging

from ..bankroll import BankrollRules, contracts_for, max_stake, size_bet
from ..config import Settings
from ..observations import Observations
from ..paper import open_paper_bet
from ..pricing import quotes, settled_result
from ..storage import Store
from ..tuning import Tuner
from .combos import edge_at

log = logging.getLogger(__name__)

KIND = "weather_today"
FRESH_MINUTES = 30
CERTAIN_PROB = 0.99
MARGIN_F = 1.5  # degrees past a market's edge before the readings count as settling it


def _event_bet(store: Store, event: str) -> bool:
    return store.conn.execute(
        "SELECT 1 FROM paper_bets WHERE kind = ? AND ticker LIKE ? LIMIT 1", (KIND, event + "-%")).fetchone() is not None


def find_today_bets(settings: Settings, store: Store, observations: Observations, rules: BankrollRules,
                    lifetime_profit: float) -> dict:
    stats = {"markets_settled_early": 0, "already_priced_in": 0, "today_bets": 0}
    if not observations.readings:
        return stats
    tuner = Tuner(store)
    if tuner.paused(KIND, 1):
        log.warning("same-day thermometer bets are paused: they have lost money over the last two weeks")
        return stats
    best: dict[str, tuple] = {}  # event -> (edge, market, side, cost, prob, observed, kind)
    for market in store.leg_markets(fresh_minutes=FRESH_MINUTES):
        event = market.get("event_ticker") or ""
        if not event or settled_result(market) or _event_bet(store, event):
            continue
        found = observations.check(market, MARGIN_F)
        if found is None:
            continue
        side, observed, kind = found
        stats["markets_settled_early"] += 1
        cost = quotes(market)[f"{side}_ask"]
        if cost is None:
            continue
        prob = tuner.adjust(KIND, 1, CERTAIN_PROB)
        edge = edge_at(prob, cost)
        if edge < settings.min_edge:
            stats["already_priced_in"] += 1
            continue
        if event not in best or edge > best[event][0]:
            best[event] = (edge, market, side, cost, prob, observed, kind)
    for event, (edge, market, side, cost, prob, observed, kind) in best.items():
        stake = size_bet(rules, prob, edge, lifetime_profit, None)
        amount = min(stake.amount, max_stake(rules, lifetime_profit))
        contracts = contracts_for(amount, cost)
        if amount <= 0 or contracts <= 0:
            continue
        reading = f"{kind} so far {observed:.1f}F"
        if not open_paper_bet(store, KIND, market["ticker"], side, cost, contracts, CERTAIN_PROB,
                              edge_at(CERTAIN_PROB, cost),
                              {"title": market.get("title"), "yes_name": market.get("yes_sub_title"),
                               "observed": observed}):
            continue
        stats["today_bets"] += 1
        store.add_decision({
            "kind": KIND, "ticker": market["ticker"], "side": side, "action": "today_bet",
            "win_prob": prob, "price": cost, "edge": edge, "stake": amount, "contracts": contracts,
            "dry_run": True,
            "reason": f"{reading} settles {market.get('yes_sub_title')} as {side.upper()}, "
                      f"Kalshi still asks {cost:.0%} ({market['ticker']})",
        })
        log.info("same-day bet %s %s: %s, Kalshi asks %.0f%%, edge %.0f%%, paper stake $%.2f",
                 market["ticker"], side, reading, cost * 100, edge * 100, amount)
    return stats

"""Pretend bets on daily temperature markets, using the NWS forecast.

For each open Kalshi high/low temperature market the forecast covers, compare
Kalshi's ask on each side with the forecast's chance. When the edge after
Kalshi's fee is at least MIN_EDGE it records a pretend bet (paper kind
"weather"). At most one bet per city-day (the best-priced bucket), and each
market is bet only once. If the forecast and Kalshi disagree wildly it is
more likely a mismatch (wrong station, odd market) than an edge, so those are
skipped. Pretend bets only; nothing here places a real order.
"""

import logging

from ..bankroll import BankrollRules, contracts_for, size_bet
from ..config import Settings
from ..paper import open_paper_bet
from ..pricing import implied_yes_prob, quotes, settled_result
from ..storage import Store
from ..tuning import Tuner
from ..weather import Forecasts
from .combos import edge_at

log = logging.getLogger(__name__)

FRESH_MINUTES = 30
MAX_GAP = 0.15


def _bet_before(store: Store, ticker: str) -> bool:
    return store.conn.execute(
        "SELECT 1 FROM paper_bets WHERE kind = 'weather' AND ticker = ? LIMIT 1", (ticker,)).fetchone() is not None


def _event_bet(store: Store, event: str) -> bool:
    return store.conn.execute(
        "SELECT 1 FROM paper_bets WHERE kind = 'weather' AND ticker LIKE ? LIMIT 1", (event + "-%",)).fetchone() is not None


def find_weather_bets(settings: Settings, store: Store, forecasts: Forecasts, rules: BankrollRules,
                      lifetime_profit: float) -> dict:
    stats = {"markets_matched": 0, "weather_bets": 0, "rejected": 0}
    if not forecasts.hourly:
        return stats
    tuner = Tuner(store)
    if tuner.paused("weather", 1):
        log.warning("weather bets are paused: they have lost money over the last two weeks")
        return stats
    best: dict[str, tuple] = {}  # event -> (edge, market, side, cost, raw_prob, prob)
    for market in store.leg_markets(fresh_minutes=FRESH_MINUTES):
        event = market.get("event_ticker") or ""
        if settled_result(market) or _bet_before(store, market["ticker"]) or _event_bet(store, event):
            continue
        p_yes = forecasts.yes_prob(market)
        if p_yes is None:
            continue
        stats["markets_matched"] += 1
        kalshi = implied_yes_prob(market)
        if kalshi is not None and abs(p_yes - kalshi) > MAX_GAP:
            stats["rejected"] += 1
            continue
        q = quotes(market)
        for side, raw_prob, cost in (("yes", p_yes, q["yes_ask"]), ("no", 1 - p_yes, q["no_ask"])):
            if cost is None:
                continue
            prob = tuner.adjust("weather", 1, raw_prob)
            edge = edge_at(prob, cost)
            if edge >= settings.min_edge and (event not in best or edge > best[event][0]):
                best[event] = (edge, market, side, cost, raw_prob, prob)
    for event, (edge, market, side, cost, raw_prob, prob) in best.items():
        stake = size_bet(rules, prob, edge, lifetime_profit, None)
        contracts = contracts_for(stake.amount, cost)
        if stake.amount <= 0 or contracts <= 0:
            continue
        if not open_paper_bet(store, "weather", market["ticker"], side, cost, contracts, raw_prob,
                              edge_at(raw_prob, cost),
                              {"title": market.get("title"), "yes_name": market.get("yes_sub_title")}):
            continue
        stats["weather_bets"] += 1
        store.add_decision({
            "kind": "weather", "ticker": market["ticker"], "side": side, "action": "weather_bet",
            "win_prob": prob, "price": cost, "edge": edge, "stake": stake.amount, "contracts": contracts,
            "dry_run": True,
            "reason": f"forecast {prob:.0%} vs Kalshi {cost:.0%}: {market.get('yes_sub_title')} ({market['ticker']})",
        })
        log.info("weather bet %s %s: forecast %.0f%% vs Kalshi %.0f%%, edge %.0f%%, paper stake $%.2f",
                 market["ticker"], side, prob * 100, cost * 100, edge * 100, stake.amount)
    return stats

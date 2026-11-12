"""Single-market value bets: Kalshi game results priced below the sportsbooks.

For each Kalshi market the sportsbooks have a price for (game winners for now),
compare Kalshi's ask on each side with the books' chance. When the edge after
Kalshi's fee is at least MIN_EDGE, record a pretend bet sized by the bankroll
rules (paper kind "pregame"; the older kind "value" came from a version that
also bet games already under way, so its results are kept apart). Only games that haven't started count (see
odds.STOP_BEFORE_START), and each market is bet at most once.
"""

import logging

from ..bankroll import BankrollRules, contracts_for, size_bet
from ..config import Settings
from ..kalshi_client import KalshiError
from ..live import live_enabled, place_live_single
from ..odds import BookOdds
from ..paper import open_paper_bet
from ..pricing import quotes, settled_result
from ..storage import Store
from ..tuning import Tuner
from .combos import edge_at

log = logging.getLogger(__name__)

# Only markets the last few scans refreshed: an old saved copy can show a
# price from before the game was decided.
FRESH_MINUTES = 30


def _bet_before(store: Store, ticker: str, event: str) -> bool:
    """One value bet per game, ever. A game has one market per team ("Lakers wins", "Clippers wins"),
    and both can look cheap: betting each one is the same bet twice, so the whole game counts once."""
    return store.conn.execute(
        "SELECT 1 FROM paper_bets WHERE kind IN ('pregame', 'value', 'live') "
        "AND (ticker = ? OR ticker LIKE ?) LIMIT 1",
        (ticker, event + "-%"),
    ).fetchone() is not None


def find_value_bets(
    settings: Settings, store: Store, book: BookOdds, rules: BankrollRules, lifetime_profit: float,
    client=None, balance: float | None = None,
) -> dict:
    stats = {"legs_matched": 0, "value_bets": 0, "bought": 0}
    live = client is not None and live_enabled(settings)
    if not book.games:
        return stats
    tuner = Tuner(store)
    if tuner.paused("pregame", 1):
        log.warning("single value bets are paused: they have lost money over the last two weeks")
        return stats
    for market in store.leg_markets(fresh_minutes=FRESH_MINUTES):
        event = market.get("event_ticker") or market["ticker"]
        if settled_result(market) or _bet_before(store, market["ticker"], event):
            continue
        p_yes = book.yes_prob(market)
        if p_yes is None:
            continue
        stats["legs_matched"] += 1
        q = quotes(market)
        for side, raw_prob, cost in (("yes", p_yes, q["yes_ask"]), ("no", 1 - p_yes, q["no_ask"])):
            if cost is None:
                continue
            prob = tuner.adjust("pregame", 1, raw_prob)  # scaled down if these bets have been overconfident
            edge = edge_at(prob, cost)
            if edge < settings.min_edge:
                continue
            # Paper bets aren't limited by the real account balance.
            stake = size_bet(rules, prob, edge, lifetime_profit, balance if live else None)
            contracts = contracts_for(stake.amount, cost)
            if stake.amount <= 0 or contracts <= 0:
                continue
            if not open_paper_bet(store, "pregame", market["ticker"], side, cost, contracts, raw_prob,
                                  edge_at(raw_prob, cost),
                                  {"title": market.get("title"), "yes_name": market.get("yes_sub_title")}):
                continue
            stats["value_bets"] += 1
            store.add_decision(
                {
                    "kind": "single",
                    "ticker": market["ticker"],
                    "side": side,
                    "action": "value_bet",
                    "win_prob": prob,
                    "price": cost,
                    "edge": edge,
                    "stake": stake.amount,
                    "contracts": contracts,
                    "dry_run": True,
                    "reason": f"sportsbooks {prob:.0%} vs Kalshi {cost:.0%}: {market.get('title')}",
                }
            )
            log.info(
                "value bet %s %s: books %.1f%% vs Kalshi %.1f%%, edge %.0f%%, paper stake $%.2f",
                market["ticker"], side, prob * 100, cost * 100, edge * 100, stake.amount,
            )
            if live and not stake.needs_approval:
                try:
                    result = place_live_single(settings, client, store, market["ticker"], side, prob, stake.amount)
                    stats["bought"] += result["status"] == "bought"
                except KalshiError as exc:
                    log.warning("live value bet %s failed: %s", market["ticker"], exc)
    return stats

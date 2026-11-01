"""Combo builder: the bot makes its own combos instead of judging other people's.

Each build:
1. Reads which events Kalshi allows in combos (the collection's event list).
2. Picks likely legs (BUILDER_MIN_LEG_PROB to BUILDER_MAX_LEG_PROB) with tight
   bid/ask spreads, at most one leg per event so legs don't depend on each other.
   Either side can be a leg, so "No: tie" counts as well as "Yes: Team A wins".
3. Creates that combo on Kalshi (free, no order) and asks market makers for a
   price, the same way as the price checks.
4. Records the result and paper trades one contract, so the scorecard can show
   which leg counts (if any) market makers underprice.

Leg counts rotate through BUILDER_LEG_COUNTS, one per build.
"""

import logging
import random
from dataclasses import dataclass

from ..collectors.markets import is_combo_market
from ..config import Settings
from ..kalshi_client import KalshiClient
from ..paper import open_paper_bet
from ..pricing import quotes
from ..storage import Store
from .combos import ComboEvaluation, edge_at
from .quotes import request_combo_price

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Leg:
    market_ticker: str
    event_ticker: str
    side: str
    prob: float
    spread: float


def candidate_legs(
    markets: list[dict],
    eligible_events: set[str] | None,
    min_prob: float,
    max_prob: float,
    max_spread: float,
    yes_only: bool = False,
) -> list[Leg]:
    """Legs whose chance is in range and whose price is tight, tightest first."""
    legs = []
    for m in markets:
        event = m.get("event_ticker")
        if not event or is_combo_market(m):
            continue
        if eligible_events is not None and event not in eligible_events:
            continue
        q = quotes(m)
        bid, ask = q["yes_bid"], q["yes_ask"]
        if bid is None or ask is None or ask < bid or ask - bid > max_spread:
            continue
        mid = (bid + ask) / 2
        for side, prob in (("yes", mid), ("no", 1 - mid)):
            if side == "no" and yes_only:
                continue
            if min_prob <= prob <= max_prob:
                legs.append(Leg(m["ticker"], event, side, round(prob, 4), round(ask - bid, 4)))
    legs.sort(key=lambda leg: leg.spread)
    return legs


def choose_legs(candidates: list[Leg], count: int, rng: random.Random, pool_size: int = 300) -> list[Leg]:
    """Pick `count` legs at random from the tightest candidates, one per event."""
    pool = candidates[:pool_size]
    rng.shuffle(pool)
    chosen, events = [], set()
    for leg in pool:
        if leg.event_ticker in events:
            continue
        chosen.append(leg)
        events.add(leg.event_ticker)
        if len(chosen) == count:
            return chosen
    return []


def build_combo(
    settings: Settings, client: KalshiClient, store: Store, rng: random.Random | None = None
) -> ComboEvaluation | None:
    rng = rng or random.Random()
    collection = client.get_collection(settings.builder_collection)
    events = collection.get("associated_event_tickers")
    eligible = set(events) if events else None
    yes_only = bool(collection.get("is_all_yes"))

    built_so_far = store.conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE action = 'built_combo'"
    ).fetchone()[0]
    count = settings.builder_leg_counts[built_so_far % len(settings.builder_leg_counts)]
    count = max(count, int(collection.get("size_min") or 2))
    if collection.get("size_max"):
        count = min(count, int(collection["size_max"]))

    candidates = candidate_legs(
        store.leg_markets(), eligible, settings.builder_min_leg_prob,
        settings.builder_max_leg_prob, settings.builder_max_spread, yes_only,
    )
    legs = choose_legs(candidates, count, rng)
    if not legs:
        log.info("combo builder: only %d usable legs, need %d", len(candidates), count)
        return None

    ticker = client.create_combo(
        settings.builder_collection,
        [{"event_ticker": l.event_ticker, "market_ticker": l.market_ticker, "side": l.side} for l in legs],
    )
    fair = 1.0
    for leg in legs:
        fair *= leg.prob
    best = request_combo_price(client, ticker, settings.start_max_stake)
    ask = best[0] if best else None
    edge = edge_at(fair, ask) if ask else None
    leg_pairs = [(l.market_ticker, l.side) for l in legs]

    store.add_decision(
        {
            "kind": "combo",
            "ticker": ticker,
            "side": "yes",
            "action": "built_combo",
            "win_prob": fair,
            "price": ask,
            "edge": edge,
            "dry_run": settings.dry_run,
            "reason": f"built {len(legs)}-leg combo" + ("" if ask else ", no quotes"),
            "raw": {"legs": leg_pairs, "leg_probs": [l.prob for l in legs], "quote": best[1] if best else None},
        }
    )
    if ask is not None and settings.dry_run:
        open_paper_bet(store, "built", ticker, "yes", ask, 1, fair, edge, {"legs": leg_pairs})
    log.info(
        "built %d-leg combo %s: fair %.2f%%, best quote %s",
        len(legs), ticker, fair * 100, "none" if ask is None else f"{ask * 100:.2f}%",
    )
    return ComboEvaluation(ticker=ticker, legs=leg_pairs, fair_prob=fair, ask=ask, bid=None, edge=edge)

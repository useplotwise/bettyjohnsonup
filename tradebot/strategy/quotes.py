"""Ask market makers for a combo price, read the replies, then withdraw.

Kalshi combos have no listed price: you send a request for quote (RFQ) and
makers reply with quotes. This never accepts a quote, so no money moves.

Kalshi's docs say each quote has "yes_bid (price per YES contract)" and
"no_bid (price per NO contract)", but not which one a buyer of YES pays
when accepting the YES side. Until a live $1 test settles it, the cost of
YES is taken as the worse (higher) of yes_bid and 1 - no_bid.
"""

import logging
import time
from typing import Callable

from ..kalshi_client import KalshiClient, KalshiError

log = logging.getLogger(__name__)


def yes_cost(quote: dict) -> float | None:
    """Conservative price to buy one YES contract from a quote, or None."""
    candidates = []
    yes_bid = float(quote.get("yes_bid_dollars") or quote.get("yes_bid") or 0)
    no_bid = float(quote.get("no_bid_dollars") or quote.get("no_bid") or 0)
    if 0 < yes_bid < 1:
        candidates.append(yes_bid)
    if 0 < no_bid < 1:
        candidates.append(1 - no_bid)
    return max(candidates) if candidates else None


def best_yes_quote(quotes: list[dict]) -> tuple[float, dict] | None:
    priced = [(yes_cost(q), q) for q in quotes if q.get("status", "open") == "open"]
    priced = [(c, q) for c, q in priced if c is not None]
    return min(priced, key=lambda cq: cq[0]) if priced else None


def request_combo_price(
    client: KalshiClient,
    market_ticker: str,
    target_cost: float,
    wait_seconds: float = 10.0,
    poll_every: float = 2.0,
    sleep: Callable[[float], None] | None = None,
) -> tuple[float, dict] | None:
    """Create an RFQ, collect quotes for up to `wait_seconds`, delete it, return the best."""
    sleep = sleep or time.sleep
    rfq_id = client.create_rfq(market_ticker, target_cost)
    best = None
    try:
        waited = 0.0
        while waited < wait_seconds:
            sleep(poll_every)
            waited += poll_every
            best = best_yes_quote(client.get_rfq_quotes(rfq_id)) or best
    finally:
        try:
            client.delete_rfq(rfq_id)
        except KalshiError as exc:
            log.warning("could not delete RFQ %s: %s", rfq_id, exc)
    return best

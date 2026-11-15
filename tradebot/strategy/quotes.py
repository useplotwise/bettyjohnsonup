"""Ask market makers for a combo price, read the replies, then withdraw.

Kalshi combos have no listed price: you send a request for quote (RFQ) and
makers reply with quotes. Nothing in this file accepts a quote, so no money moves.

How to read a quote (settled by the bot's first 16 real bets, Nov 12-15 2026,
and by Kalshi's docs: "BUY accepts the maker's NO quote and SELL accepts the
maker's YES quote"):

- `yes_bid` is what the MAKER pays for YES. Accepting the "yes" side sells YES
  to the maker, which leaves us holding NO at 1 - yes_bid. It is never a price
  we can buy YES at.
- `no_bid` is what the maker pays for NO. Accepting the "no" side is how we buy
  YES, and it costs 1 - no_bid a contract, for `no_contracts_fp` contracts.
- A bid of 0 means the maker declined that side. Many makers only bid for YES
  (no_bid 0): those quotes do not sell YES at any price.

Earlier versions took yes_bid as the cost of YES and accepted the "yes" side,
so every real combo bet bought NO by mistake.
"""

import logging
import time
from typing import Callable

from ..kalshi_client import KalshiClient, KalshiError

log = logging.getLogger(__name__)

# The side of a quote to accept in order to end up holding YES.
YES_ACCEPT_SIDE = "no"


def _bid(quote: dict, side: str) -> float:
    try:
        return float(quote.get(f"{side}_bid_dollars") or quote.get(f"{side}_bid") or 0)
    except (TypeError, ValueError):
        return 0.0


def yes_cost(quote: dict) -> float | None:
    """Price to buy one YES contract from this quote, or None if it doesn't sell YES."""
    no_bid = _bid(quote, "no")
    return round(1 - no_bid, 4) if 0 < no_bid < 1 else None


def yes_size(quote: dict) -> float | None:
    """How many YES contracts accepting this quote buys, or None if the quote doesn't say."""
    try:
        size = float(quote.get("no_contracts_fp") or 0)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def best_yes_quote(quotes: list[dict]) -> tuple[float, dict] | None:
    """The cheapest open quote that sells YES, as (cost per contract, quote)."""
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
    seen: list | None = None,
) -> tuple[float, dict] | None:
    """Create an RFQ, collect quotes for up to `wait_seconds`, delete it, return the best.

    Every quote received, including ones that don't sell YES, is added to `seen`.
    """
    sleep = sleep or time.sleep
    rfq_id = client.create_rfq(market_ticker, target_cost)
    best = None
    try:
        waited = 0.0
        while waited < wait_seconds:
            sleep(poll_every)
            waited += poll_every
            quotes = client.get_rfq_quotes(rfq_id)
            if seen is not None and quotes:
                seen[:] = quotes
            best = best_yes_quote(quotes) or best
    finally:
        try:
            client.delete_rfq(rfq_id)
        except KalshiError as exc:
            log.warning("could not delete RFQ %s: %s", rfq_id, exc)
    return best

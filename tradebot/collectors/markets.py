"""Collect open Kalshi markets and order books."""

import logging

from ..kalshi_client import KalshiClient, KalshiError
from ..storage import Store, utcnow

log = logging.getLogger(__name__)


def is_combo_market(market: dict) -> bool:
    """Kalshi's multi-leg combos are multivariate-event markets (MVE)."""
    return bool(
        market.get("mve_collection_ticker")
        or market.get("mve_selected_legs")
        or str(market.get("ticker", "")).upper().startswith("KXMVE")
    )


def normalize_market(market: dict) -> dict:
    return {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "series_ticker": market.get("series_ticker"),
        "category": market.get("category"),
        "title": market.get("title"),
        "status": market.get("status"),
        "is_combo": int(is_combo_market(market)),
        "close_time": market.get("close_time"),
        "result": market.get("result") or None,
        "raw": market,
    }


def summarize_orderbook(ticker: str, book: dict) -> dict:
    """Order book levels are [price_cents, quantity]; Kalshi lists bids only.

    A YES ask is implied by the best NO bid: yes_ask = 100 - best_no_bid.
    """
    yes = book.get("yes") or []
    no = book.get("no") or []
    best_yes = max((p for p, _ in yes), default=None)
    best_no = max((p for p, _ in no), default=None)
    return {
        "ticker": ticker,
        "taken_at": utcnow(),
        "best_yes_bid": best_yes,
        "best_no_bid": best_no,
        "yes_ask": None if best_no is None else 100 - best_no,
        "yes_depth": sum(q for _, q in yes),
        "no_depth": sum(q for _, q in no),
        "raw": book,
    }


def collect_markets(client: KalshiClient, store: Store, max_markets: int | None = None) -> list[dict]:
    rows = []
    for m in client.iter_markets(status="open"):
        rows.append(normalize_market(m))
        if len(rows) % 1000 == 0:
            log.info("read %d markets so far...", len(rows))
        if max_markets and len(rows) >= max_markets:
            log.info("reached the %d-market limit for this scan", max_markets)
            break
    store.upsert_markets(rows)
    log.info("stored %d open markets (%d combos)", len(rows), sum(r["is_combo"] for r in rows))
    return rows


def collect_orderbooks(
    client: KalshiClient, store: Store, tickers: list[str], max_books: int = 200
) -> int:
    """Snapshot order books, skipping any market whose book fails to load."""
    taken = 0
    batch = tickers[:max_books]
    log.info("reading %d order books...", len(batch))
    for i, ticker in enumerate(batch, 1):
        if i % 25 == 0:
            log.info("order books: %d of %d", i, len(batch))
        try:
            book = client.get_orderbook(ticker)
        except KalshiError as exc:
            log.warning("orderbook %s: %s", ticker, exc)
            continue
        store.add_orderbook_snapshot(summarize_orderbook(ticker, book))
        taken += 1
    return taken

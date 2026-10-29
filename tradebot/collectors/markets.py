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


def _levels(book: dict, side: str) -> list[tuple[int, float]]:
    """Return [(price_cents, quantity)] for one side, from either response format."""
    if book.get(side):
        return [(int(p), float(q)) for p, q in book[side]]
    if book.get(f"{side}_dollars"):
        return [(round(float(p) * 100), float(q)) for p, q in book[f"{side}_dollars"]]
    return []


def summarize_orderbook(ticker: str, book: dict) -> dict:
    """Kalshi lists bids only. A YES ask is implied by the best NO bid:
    yes_ask = 100 - best_no_bid (in cents).
    """
    yes = _levels(book, "yes")
    no = _levels(book, "no")
    best_yes = max((p for p, _ in yes), default=None)
    best_no = max((p for p, _ in no), default=None)
    return {
        "ticker": ticker,
        "taken_at": utcnow(),
        "best_yes_bid": best_yes,
        "best_no_bid": best_no,
        "yes_ask": None if best_no is None else 100 - best_no,
        "yes_depth": round(sum(q for _, q in yes)),
        "no_depth": round(sum(q for _, q in no)),
        "raw": book,
    }


def _take(markets, limit: int, label: str) -> list[dict]:
    rows = []
    for m in markets:
        rows.append(normalize_market(m))
        if len(rows) % 1000 == 0:
            log.info("read %d %s so far...", len(rows), label)
        if limit and len(rows) >= limit:
            log.info("reached the %d limit for %s this scan", limit, label)
            break
    return rows


def collect_markets(
    client: KalshiClient, store: Store, max_markets: int | None = None, max_combos: int = 2000
) -> list[dict]:
    """Read regular markets and combo markets separately.

    Kalshi lists thousands of combo markets first, so an unfiltered scan never
    reaches weather, politics and the rest. The mve_filter parameter splits them;
    if the API rejects it, fall back to one unfiltered scan.
    """
    try:
        others = _take(client.iter_markets(status="open", mve_filter="exclude"), max_markets or 0, "regular markets")
        combos = _take(client.iter_markets(status="open", mve_filter="only"), max_combos, "combo markets")
    except KalshiError as exc:
        log.warning("filtered market scan failed (%s); scanning without filter", exc)
        others, combos = _take(client.iter_markets(status="open"), max_markets or 0, "markets"), []
    if any(r["is_combo"] for r in others):
        log.warning("the regular-market scan returned combos; mve_filter may be ignored")
    # De-duplicate in case the filter was ignored and both scans overlap.
    rows = list({r["ticker"]: r for r in combos + others}.values())
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
            snap = summarize_orderbook(ticker, book)
        except (KalshiError, ValueError, TypeError) as exc:
            log.warning("orderbook %s: %s", ticker, exc)
            continue
        store.add_orderbook_snapshot(snap)
        taken += 1
    return taken

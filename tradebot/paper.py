"""Paper trading: pretend bets that are settled against real results.

Two kinds of pretend bet are kept apart:

- "pick": a bet the bot would have placed (a would_buy decision), at its stake.
- "watch": one contract of every combo a market maker quoted a price for,
  whether or not the bot liked it. This shows whether the bot's fair-chance
  estimates are right, and whether passing on those quotes was the right call.

No money moves. Each cycle the bot looks up open pretend bets and, once Kalshi
settles a market, records whether it won and the profit or loss after fees.
"""

import json
import logging

from .kalshi_client import KalshiClient, KalshiError
from .pricing import kalshi_fee, settled_result
from .storage import Store, utcnow

log = logging.getLogger(__name__)

SETTLED_STATUSES = {"settled", "finalized", "determined"}


def open_paper_bet(
    store: Store,
    kind: str,
    ticker: str,
    side: str,
    price: float,
    contracts: int,
    win_prob: float | None = None,
    edge: float | None = None,
    raw: dict | None = None,
) -> bool:
    """Record a pretend bet. Returns False if the same kind is already open on this ticker."""
    if contracts <= 0 or not 0 < price < 1:
        return False
    exists = store.conn.execute(
        "SELECT 1 FROM paper_bets WHERE kind = ? AND ticker = ? AND status = 'open'", (kind, ticker)
    ).fetchone()
    if exists:
        return False
    with store.conn:
        store.conn.execute(
            """INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee,
                   win_prob, edge, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (utcnow(), kind, ticker, side, price, contracts, kalshi_fee(price, contracts),
             win_prob, edge, json.dumps(raw) if raw is not None else None),
        )
    return True


def settle_paper_bets(client: KalshiClient, store: Store, max_checks: int = 50) -> dict:
    """Check open pretend bets (least recently checked first) and settle finished ones."""
    stats = {"open": 0, "checked": 0, "settled": 0}
    rows = store.conn.execute(
        "SELECT * FROM paper_bets WHERE status = 'open' ORDER BY checked_at IS NOT NULL, checked_at"
    ).fetchall()
    stats["open"] = len(rows)
    markets: dict[str, dict | None] = {}
    for row in rows[:max_checks]:
        ticker = row["ticker"]
        if ticker not in markets:
            try:
                markets[ticker] = client.get_market(ticker)
            except KalshiError as exc:
                log.debug("paper bet %s: %s", ticker, exc)
                markets[ticker] = None
        market = markets[ticker]
        stats["checked"] += 1
        now = utcnow()
        if market is None:
            with store.conn:
                store.conn.execute("UPDATE paper_bets SET checked_at = ? WHERE id = ?", (now, row["id"]))
            continue
        result = settled_result(market)
        cost = row["price"] * row["contracts"] + row["fee"]
        if result is not None:
            status = "won" if result == row["side"] else "lost"
            pnl = (row["contracts"] if status == "won" else 0) - cost
        elif (market.get("status") or "").lower() in SETTLED_STATUSES:
            status, pnl = "void", 0.0  # settled with no yes/no result: stake returned
        else:
            with store.conn:
                store.conn.execute("UPDATE paper_bets SET checked_at = ? WHERE id = ?", (now, row["id"]))
            continue
        with store.conn:
            store.conn.execute(
                "UPDATE paper_bets SET status = ?, pnl = ?, checked_at = ?, settled_at = ? WHERE id = ?",
                (status, round(pnl, 2), now, now, row["id"]),
            )
        stats["settled"] += 1
        log.info("paper %s %s settled: %s, P&L $%.2f", row["kind"], ticker, status, pnl)
    return stats


def paper_summary(store: Store, kind: str) -> dict:
    """Lifetime totals for one kind of pretend bet."""
    rows = store.conn.execute("SELECT * FROM paper_bets WHERE kind = ?", (kind,)).fetchall()
    settled = [r for r in rows if r["status"] in ("won", "lost")]
    won = [r for r in settled if r["status"] == "won"]
    probs = [r["win_prob"] for r in settled if r["win_prob"] is not None]
    return {
        "open": sum(r["status"] == "open" for r in rows),
        "settled": len(settled),
        "won": len(won),
        "staked": round(sum(r["price"] * r["contracts"] + r["fee"] for r in settled), 2),
        "pnl": round(sum(r["pnl"] or 0 for r in settled), 2),
        "expected_win_rate": sum(probs) / len(probs) if probs else None,
        "actual_win_rate": len(won) / len(settled) if settled else None,
    }

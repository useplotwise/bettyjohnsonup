"""Real-money betting (Phase 4).

Nothing here runs unless DRY_RUN=false AND BOT_BANKROLL is above 0. Then, for
each pick the bot would make, it:

1. Checks the bot's own money: BOT_BANKROLL plus its settled profit, minus what
   is tied up in its open bets. It never touches the rest of your balance.
2. Checks the daily limit (MAX_LIVE_BETS_PER_DAY).
3. Asks market makers for a fresh price for exactly the stake.
4. Accepts the best quote only if it still clears MIN_EDGE at that price.
5. Waits for the maker to confirm, then records the bet (kind "live" in the
   paper_bets table, so it settles the same way) using the price actually
   filled, read from Kalshi's fills.

Bets over the approval limit are never placed; they stay "needs_approval".
"""

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .kalshi_client import KalshiClient, KalshiError
from .paper import open_paper_bet
from .storage import Store
from .strategy.combos import edge_at
from .strategy.quotes import best_yes_quote

log = logging.getLogger(__name__)

WAIT_FOR_QUOTES = 10.0
WAIT_FOR_CONFIRM = 12.0
POLL_EVERY = 2.0


def live_enabled(settings: Settings) -> bool:
    return not settings.dry_run and settings.bot_bankroll > 0 and settings.has_credentials


def bot_ledger(store: Store) -> dict:
    """The bot's real-money record: settled profit, money in open bets, bets today."""
    rows = store.conn.execute("SELECT * FROM paper_bets WHERE kind = 'live'").fetchall()
    today = datetime.now(timezone.utc).date().isoformat()
    settled = [r for r in rows if r["status"] in ("won", "lost", "void")]
    return {
        "profit": round(sum(r["pnl"] or 0 for r in settled), 2),
        "in_open_bets": round(
            sum(r["price"] * r["contracts"] + r["fee"] for r in rows if r["status"] == "open"), 2
        ),
        "bets_today": sum(r["opened_at"] >= today for r in rows),
        "open": sum(r["status"] == "open" for r in rows),
        "settled": len(settled),
        "won": sum(r["status"] == "won" for r in rows),
    }


def money_available(settings: Settings, store: Store) -> float:
    ledger = bot_ledger(store)
    return round(settings.bot_bankroll + ledger["profit"] - ledger["in_open_bets"], 2)


def _filled_price(client: KalshiClient, ticker: str) -> float | None:
    """Average YES price we actually paid on this combo, from Kalshi's fills."""
    try:
        fills = client.get_fills(ticker=ticker).get("fills", [])
    except KalshiError as exc:
        log.warning("could not read fills for %s: %s", ticker, exc)
        return None
    total = cost = 0.0
    for f in fills:
        if (f.get("side") or "yes") != "yes" or (f.get("action") or "buy") != "buy":
            continue
        count = float(f.get("count_fp") or f.get("count") or 0)
        if f.get("yes_price_dollars") not in (None, ""):
            price = float(f["yes_price_dollars"])
        elif f.get("yes_price") not in (None, ""):
            price = float(f["yes_price"]) / 100
        else:
            continue
        total += count
        cost += count * price
    return round(cost / total, 4) if total else None


def place_live_bet(
    settings: Settings,
    client: KalshiClient,
    store: Store,
    ticker: str,
    fair_prob: float,
    stake: float,
    legs: list | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Try to buy YES on a combo for about `stake` dollars. Returns what happened."""
    sleep = sleep or time.sleep
    result = {"status": "skipped", "reason": "", "price": None, "contracts": 0.0, "stake": stake}

    ledger = bot_ledger(store)
    if ledger["bets_today"] >= settings.max_live_bets_per_day:
        result["reason"] = f"daily limit of {settings.max_live_bets_per_day} bets reached"
    else:
        stake = min(stake, money_available(settings, store))
        result["stake"] = stake
        if stake < settings.min_stake:
            result["reason"] = f"bot money left (${stake:.2f}) is below the ${settings.min_stake:.0f} minimum"
    if result["reason"]:
        _record(settings, store, ticker, fair_prob, result, legs)
        return result

    rfq_id = client.create_rfq(ticker, stake)
    try:
        best, waited = None, 0.0
        while waited < WAIT_FOR_QUOTES:
            sleep(POLL_EVERY)
            waited += POLL_EVERY
            best = best_yes_quote(client.get_rfq_quotes(rfq_id)) or best
        if best is None:
            result["reason"] = "no quotes"
            return result
        cost, quote = best
        edge = edge_at(fair_prob, cost)
        if edge < settings.min_edge:
            result["reason"] = f"fresh price {cost:.2f} leaves edge {edge:.0%}, below {settings.min_edge:.0%}"
            return result

        client.accept_quote(rfq_id, quote["id"], "yes")
        status, waited = "accepted", 0.0
        while waited < WAIT_FOR_CONFIRM and status not in ("executed", "cancelled"):
            sleep(POLL_EVERY)
            waited += POLL_EVERY
            quote = client.get_quote(rfq_id, quote["id"]) or quote
            status = quote.get("status", status)
        if status != "executed":
            result["reason"] = f"maker did not confirm (status {status})"
            return result

        contracts = float(quote.get("yes_contracts_fp") or quote.get("contracts_fp") or 0)
        price = _filled_price(client, ticker) or cost
        result.update(status="bought", price=price, contracts=contracts,
                      reason=f"bought {contracts:g} at ${price:.2f}, fair chance {fair_prob:.1%}")
        open_paper_bet(store, "live", ticker, "yes", price, contracts, fair_prob, edge_at(fair_prob, price),
                       {"legs": legs or [], "quote_id": quote["id"], "quoted_cost": cost})
        return result
    finally:
        try:
            client.delete_rfq(rfq_id)
        except KalshiError:
            pass  # already closed once the trade went through
        _record(settings, store, ticker, fair_prob, result, legs)
        log.warning("LIVE %s %s: %s", result["status"], ticker, result["reason"])


def _record(settings: Settings, store: Store, ticker: str, fair_prob: float, result: dict, legs) -> None:
    store.add_decision(
        {
            "kind": "combo",
            "ticker": ticker,
            "side": "yes",
            "action": "bought" if result["status"] == "bought" else "live_skipped",
            "win_prob": fair_prob,
            "price": result["price"],
            "stake": result["stake"],
            "contracts": result["contracts"],
            "dry_run": False,
            "reason": result["reason"],
            "raw": {"legs": legs or []},
        }
    )

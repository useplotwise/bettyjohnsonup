"""Real-money betting (Phase 4).

Nothing here runs unless DRY_RUN=false AND BOT_BANKROLL is above 0. Then, for
each pick the bot would make, it:

1. Checks the bot's own money: BOT_BANKROLL plus its settled profit, minus what
   is tied up in its open bets. It never touches the rest of your balance.
2. Checks the daily limit (MAX_LIVE_BETS_PER_DAY) and caps the stake at
   LIVE_MAX_STAKE ($1 by default, for the first test stage).
3. Asks market makers for a fresh price for exactly the stake.
4. Accepts the best quote only if it sells YES, still clears MIN_EDGE at that
   price, and costs no more than the stake.
5. Waits for the maker to confirm, then reads Kalshi's own record of the trade
   (the fills) and records the bet (kind "live" in the paper_bets table, so it
   settles the same way) with the side and price Kalshi reports.
6. If Kalshi reports anything other than "bought YES at about the quoted
   price", or the trade can't be found, ALL real betting stops until a person
   has looked (see halt_live and `python -m tradebot.resume_live`).

Bets over the approval limit are never placed; they stay "needs_approval".
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .kalshi_client import KalshiClient, KalshiError
from .paper import open_paper_bet
from .pricing import kalshi_fee, quotes
from .storage import Store, utcnow
from .strategy.combos import edge_at
from .strategy.quotes import YES_ACCEPT_SIDE, best_yes_quote, yes_size

log = logging.getLogger(__name__)

WAIT_FOR_QUOTES = 10.0
WAIT_FOR_CONFIRM = 12.0
WAIT_FOR_FILLS = 10.0
POLL_EVERY = 2.0
# A fill this much above the quoted price counts as "not what we agreed to".
PRICE_TOLERANCE = 0.011


def live_enabled(settings: Settings) -> bool:
    return not settings.dry_run and settings.bot_bankroll > 0 and settings.has_credentials


def bot_ledger(store: Store) -> dict:
    """The bot's real-money record: settled profit, money in open bets, bets today."""
    rows = store.conn.execute("SELECT * FROM paper_bets WHERE kind = 'live'").fetchall()
    today = datetime.now(timezone.utc).date().isoformat()
    settled = [r for r in rows if r["status"] in ("won", "lost", "void", "sold")]
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


def real_pnl_today(store: Store) -> float:
    """Profit (negative = loss) of real bets that finished today (UTC)."""
    today = datetime.now(timezone.utc).date().isoformat()
    row = store.conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_bets WHERE kind = 'live' "
        "AND status IN ('won', 'lost', 'void', 'sold') AND settled_at >= ?", (today,)
    ).fetchone()
    return round(row[0], 2)


def halted(store: Store) -> str:
    """Why real betting was halted by a check on a finished trade, or "" when it isn't."""
    row = store.conn.execute(
        "SELECT action, reason FROM decisions WHERE kind = 'safety' "
        "AND action IN ('live_halt', 'live_resume') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (row["reason"] or "a trade did not check out") if row and row["action"] == "live_halt" else ""


def halt_live(store: Store, ticker: str, reason: str) -> None:
    """Stop all real betting until a person clears it with `python -m tradebot.resume_live`."""
    store.add_decision({"kind": "safety", "ticker": ticker, "action": "live_halt",
                        "dry_run": False, "reason": reason})
    log.error("REAL BETTING HALTED after %s: %s", ticker, reason)


def resume_live(store: Store) -> bool:
    """Clear a halt. Returns False if there was nothing to clear."""
    if not halted(store):
        return False
    store.add_decision({"kind": "safety", "ticker": "-", "action": "live_resume",
                        "dry_run": False, "reason": "halt cleared by hand"})
    return True


def safety_stop(settings: Settings, store: Store) -> str:
    """Why real betting is stopped or paused, or "" when it isn't."""
    reason = halted(store)
    if reason:
        return f"stopped until you clear it: {reason}"
    bankroll = settings.bot_bankroll
    profit = bot_ledger(store)["profit"]
    if settings.max_drawdown_fraction > 0 and profit <= -settings.max_drawdown_fraction * bankroll:
        return (f"stopped: real bets are down ${-profit:.2f}, past the "
                f"{settings.max_drawdown_fraction:.0%} limit of ${bankroll:.2f}")
    today = real_pnl_today(store)
    if settings.max_daily_loss_fraction > 0 and today <= -settings.max_daily_loss_fraction * bankroll:
        return (f"paused for today: lost ${-today:.2f} today, past the "
                f"{settings.max_daily_loss_fraction:.0%} daily limit of ${bankroll:.2f}")
    return ""


def money_available(settings: Settings, store: Store) -> float:
    ledger = bot_ledger(store)
    return round(settings.bot_bankroll + ledger["profit"] - ledger["in_open_bets"], 2)


def _fills(client: KalshiClient, side: str = "yes", action: str = "buy", **params) -> tuple[float, float | None]:
    """(contracts filled, average price) for buys or sells on `side`, from Kalshi's fills."""
    try:
        fills = client.get_fills(**params).get("fills", [])
    except KalshiError as exc:
        log.warning("could not read fills for %s: %s", params, exc)
        return 0.0, None
    total = cost = 0.0
    for f in fills:
        if (f.get("side") or "yes") != side or (f.get("action") or "buy") != action:
            continue
        count = float(f.get("count_fp") or f.get("count") or 0)
        if f.get(f"{side}_price_dollars") not in (None, ""):
            price = float(f[f"{side}_price_dollars"])
        elif f.get(f"{side}_price") not in (None, ""):
            price = float(f[f"{side}_price"]) / 100
        else:
            continue
        total += count
        cost += count * price
    return total, (round(cost / total, 4) if total else None)


def _fill_id(fill: dict) -> str:
    return str(fill.get("fill_id") or fill.get("trade_id") or sorted(fill.items()))


def _fill_price(fill: dict, side: str) -> float | None:
    if fill.get(f"{side}_price_dollars") not in (None, ""):
        return float(fill[f"{side}_price_dollars"])
    if fill.get(f"{side}_price") not in (None, ""):
        return float(fill[f"{side}_price"]) / 100
    return None


def _fill_ids(client: KalshiClient, ticker: str) -> set[str] | None:
    """Ids of every fill Kalshi already has on this market, or None if they can't be read."""
    try:
        return {_fill_id(f) for f in client.get_fills(ticker=ticker).get("fills", [])}
    except KalshiError as exc:
        log.warning("could not read fills for %s: %s", ticker, exc)
        return None


def _new_fills(client: KalshiClient, ticker: str, before: set[str]) -> list[dict]:
    """What Kalshi says we traded on this market since `before`: side, action, count, price."""
    try:
        fills = client.get_fills(ticker=ticker).get("fills", [])
    except KalshiError as exc:
        log.warning("could not read fills for %s: %s", ticker, exc)
        return []
    found = []
    for f in fills:
        if _fill_id(f) in before:
            continue
        side = (f.get("side") or "").lower()
        price = _fill_price(f, side)
        if price is None and side in ("yes", "no"):  # Kalshi usually gives both; one implies the other
            other = _fill_price(f, "no" if side == "yes" else "yes")
            price = None if other is None else round(1 - other, 4)
        found.append({"side": side, "action": (f.get("action") or "").lower(),
                      "count": float(f.get("count_fp") or f.get("count") or 0), "price": price})
    return found


def _check_fills(fills: list[dict], quoted_cost: float) -> tuple[str, float, float | None, str]:
    """(side, contracts, average price, what is wrong or "") for the fills of one accepted quote."""
    if not fills:
        return "yes", 0.0, None, "Kalshi shows no trade for a quote the maker confirmed"
    sides = {(f["side"], f["action"]) for f in fills}
    side = fills[0]["side"] or "yes"
    contracts = sum(f["count"] for f in fills if f["side"] == side)
    priced = [f for f in fills if f["side"] == side and f["price"] is not None]
    price = round(sum(f["count"] * f["price"] for f in priced) / sum(f["count"] for f in priced), 4) \
        if priced and sum(f["count"] for f in priced) else None
    if sides != {("yes", "buy")}:
        got = ", ".join(sorted(f"{a or '?'} {sd.upper() or '?'}" for sd, a in sides))
        return side, contracts, price, f"meant to buy YES but Kalshi says: {got}"
    if price is None:
        return side, contracts, price, "Kalshi's record of the trade has no price"
    if price > quoted_cost + PRICE_TOLERANCE:
        return side, contracts, price, f"paid ${price:.2f} a contract, quoted ${quoted_cost:.2f}"
    return side, contracts, price, ""


def _money_check(settings: Settings, store: Store, stake: float) -> tuple[float, str]:
    """(stake allowed, reason it isn't) given the loss stops, daily limit and the bot's own money."""
    stopped = safety_stop(settings, store)
    if stopped:
        return stake, stopped
    if bot_ledger(store)["bets_today"] >= settings.max_live_bets_per_day:
        return stake, f"daily limit of {settings.max_live_bets_per_day} bets reached"
    stake = min(stake, settings.live_max_stake, money_available(settings, store))
    minimum = min(settings.min_stake, settings.live_max_stake)
    if stake < minimum:
        return stake, f"bot money left (${stake:.2f}) is below the ${minimum:.2f} minimum"
    return stake, ""


def place_live_single(
    settings: Settings,
    client: KalshiClient,
    store: Store,
    ticker: str,
    side: str,
    fair_prob: float,
    stake: float,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Buy `side` on a single (non-combo) market for about `stake` dollars.

    Re-reads the price, places a limit order at that price (never higher),
    waits briefly, cancels whatever didn't fill, and records what did.
    """
    sleep = sleep or time.sleep
    result = {"status": "skipped", "reason": "", "price": None, "contracts": 0.0, "stake": stake}
    stake, result["reason"] = _money_check(settings, store, stake)
    result["stake"] = stake
    if not result["reason"]:
        market = client.get_market(ticker)
        price = quotes(market)[f"{side}_ask"]
        if price is None or not 0 < price < 1:
            result["reason"] = "no price to buy at"
        elif edge_at(fair_prob, price) < settings.min_edge:
            result["reason"] = f"fresh price {price:.2f} leaves edge {edge_at(fair_prob, price):.0%}"
        else:
            count = int(stake // price)
            if count < 1:
                result["reason"] = "stake buys less than one contract"
            else:
                order = client.create_order(ticker, side, count, price, str(uuid.uuid4()))
                order_id = order.get("order_id")
                sleep(WAIT_FOR_CONFIRM / 2)
                if order_id:
                    try:
                        client.cancel_order(order_id)  # cancel any part that didn't fill
                    except KalshiError:
                        pass  # already fully filled (nothing left to cancel)
                filled, paid = _fills(client, side, order_id=order_id) if order_id else (0.0, None)
                if filled > 0:
                    paid = paid or price
                    result.update(status="bought", price=paid, contracts=filled,
                                  reason=f"bought {filled:g} {side.upper()} at ${paid:.2f}, "
                                         f"fair chance {fair_prob:.1%}")
                    open_paper_bet(store, "live", ticker, side, paid, int(filled), fair_prob,
                                   edge_at(fair_prob, paid), {"order_id": order_id, "single": True})
                else:
                    result["reason"] = f"order at ${price:.2f} didn't fill"
    _record(settings, store, ticker, fair_prob, result, None, side=side, kind="single")
    log.warning("LIVE %s %s %s: %s", result["status"], ticker, side, result["reason"])
    return result


def sell_live_position(
    settings: Settings,
    client: KalshiClient,
    store: Store,
    row,
    bid: float,
    reason: str,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Sell a single-market bet the bot owns ("cash out") with a limit order at `bid` or better.

    Waits briefly, cancels whatever didn't fill, then closes the filled part in
    the ledger. A partial fill splits the bet: the sold part is closed with its
    profit, the rest stays open. Only ever called for the bot's own bets.
    """
    sleep = sleep or time.sleep
    ticker, side = row["ticker"], row["side"]
    result = {"status": "skipped", "reason": "", "price": None, "contracts": 0.0}
    order = client.create_order(ticker, side, int(row["contracts"]), bid, str(uuid.uuid4()), action="sell")
    order_id = order.get("order_id")
    sleep(WAIT_FOR_CONFIRM / 2)
    if order_id:
        try:
            client.cancel_order(order_id)  # cancel any part that didn't fill
        except KalshiError:
            pass  # already fully filled
    filled, got = _fills(client, side, action="sell", order_id=order_id) if order_id else (0.0, None)
    if filled > 0:
        got = got or bid
        _close_sold(store, row, int(filled), got)
        result.update(status="sold", price=got, contracts=filled,
                      reason=f"sold {filled:g} {side.upper()} at ${got:.2f}: {reason}")
    else:
        result["reason"] = f"sell order at ${bid:.2f} didn't fill"
    store.add_decision(
        {
            "kind": "cashout",
            "ticker": ticker,
            "side": side,
            "action": "sold" if result["status"] == "sold" else "sell_skipped",
            "price": result["price"],
            "contracts": result["contracts"],
            "dry_run": False,
            "reason": result["reason"],
        }
    )
    log.warning("LIVE cash-out %s %s: %s", result["status"], ticker, result["reason"])
    return result


def _close_sold(store: Store, row, filled: int, price: float) -> None:
    contracts = int(row["contracts"])
    filled = min(filled, contracts)
    fee_part = round(row["fee"] * filled / contracts, 4)
    cost = row["price"] * filled + fee_part
    pnl = round(filled * price - kalshi_fee(price, filled) - cost, 2)
    now = utcnow()
    with store.conn:
        if filled >= contracts:
            store.conn.execute(
                "UPDATE paper_bets SET status = 'sold', pnl = ?, checked_at = ?, settled_at = ? WHERE id = ?",
                (pnl, now, now, row["id"]),
            )
        else:
            store.conn.execute(
                "UPDATE paper_bets SET contracts = contracts - ?, fee = fee - ? WHERE id = ?",
                (filled, fee_part, row["id"]),
            )
            store.conn.execute(
                "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee, win_prob, edge, "
                "status, pnl, checked_at, settled_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sold', ?, ?, ?, ?)",
                (row["opened_at"], row["kind"], row["ticker"], row["side"], row["price"], filled, fee_part,
                 row["win_prob"], row["edge"], pnl, now, now, row["raw"]),
            )


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

    stake, result["reason"] = _money_check(settings, store, stake)
    result["stake"] = stake
    if result["reason"]:
        _record(settings, store, ticker, fair_prob, result, legs)
        return result

    # Kalshi's record of the trade is checked afterwards, so it must be readable first.
    before = _fill_ids(client, ticker)
    if before is None:
        result["reason"] = "can't read Kalshi's trade record, so not betting"
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
            result["reason"] = "no quote that sells YES"
            return result
        cost, quote = best
        log.info("LIVE quote for %s: %s", ticker, quote)
        edge = edge_at(fair_prob, cost)
        if edge < settings.min_edge:
            result["reason"] = f"fresh price {cost:.2f} leaves edge {edge:.0%}, below {settings.min_edge:.0%}"
            return result

        # Accepting fills whatever the maker quoted, so check its size first. A quote
        # that doesn't show its size, or costs more than the stake, is never accepted.
        size = yes_size(quote)
        if size is None:
            result["reason"] = "quote does not show its size, not accepting"
            return result
        quoted_total = size * cost + kalshi_fee(cost, size)
        if quoted_total > stake + 0.005:
            result["reason"] = f"quote costs ${quoted_total:.2f}, above the ${stake:.2f} stake"
            return result

        client.accept_quote(rfq_id, quote["id"], YES_ACCEPT_SIDE)
        status, waited = "accepted", 0.0
        while waited < WAIT_FOR_CONFIRM and status not in ("executed", "cancelled"):
            sleep(POLL_EVERY)
            waited += POLL_EVERY
            quote = client.get_quote(rfq_id, quote["id"]) or quote
            status = quote.get("status", status)
        fills = _new_fills(client, ticker, before)
        if status != "executed" and not fills:
            result["reason"] = f"maker did not confirm (status {status})"
            return result

        # The trade happened. Believe Kalshi's record of it, not the quote.
        waited = 0.0
        while not fills and waited < WAIT_FOR_FILLS:
            sleep(POLL_EVERY)
            waited += POLL_EVERY
            fills = _new_fills(client, ticker, before)
        side, contracts, price, problem = _check_fills(fills, cost)
        contracts, price = contracts or size, price if price is not None else cost
        won_prob = fair_prob if side == "yes" else 1 - fair_prob
        result.update(status="bought", price=price, contracts=contracts,
                      reason=f"bought {contracts:g} {side.upper()} at ${price:.2f}, fair chance {won_prob:.1%}")
        open_paper_bet(store, "live", ticker, side, price, contracts, won_prob, edge_at(won_prob, price),
                       {"legs": legs or [], "quote_id": quote["id"], "quoted_cost": cost,
                        "side_from_fills": bool(fills), **({"problem": problem} if problem else {})})
        if problem:
            result["reason"] += f". PROBLEM: {problem}"
            halt_live(store, ticker, problem)
        return result
    finally:
        try:
            client.delete_rfq(rfq_id)
        except KalshiError:
            pass  # already closed once the trade went through
        _record(settings, store, ticker, fair_prob, result, legs)
        log.warning("LIVE %s %s: %s", result["status"], ticker, result["reason"])


def _record(settings: Settings, store: Store, ticker: str, fair_prob: float, result: dict, legs,
            side: str = "yes", kind: str = "combo") -> None:
    store.add_decision(
        {
            "kind": kind,
            "ticker": ticker,
            "side": side,
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

"""Decide when to sell ("cash out") a position before it settles.

Kalshi has no sportsbook-style cash-out offer; the equivalent is selling the
contracts at the current bid. Two rules, either one triggers a sale:

1. EV preservation: selling now is worth at least as much as holding
   (bid after fees >= our estimated chance of the remaining legs hitting).
2. Lock in profit: the sale returns at least LOCK_MULTIPLE x what we paid,
   and the remaining legs are more likely to miss than hit.

What "holding is worth" (hold_probability) is the bot's own estimate:
- A combo: the product of its legs' current chances.
- A single bet: the lower of the bot's original estimate (scaled by self-tuning)
  and the market's current price, so a falling price is never treated as a
  reason to panic-sell, and a price that ran past our estimate is.

Pretend bets are only flagged (PaperCashOuts) and later compared with what
holding earned. Real cash-outs (live_cash_outs) sell only single-market bets
the bot itself placed, never the rest of your Kalshi account, and combos are
only flagged because they have no order book to sell into.
"""

import json
import logging
from dataclasses import dataclass

from ..config import Settings
from ..kalshi_client import KalshiClient, KalshiError
from ..live import live_enabled, sell_live_position
from ..pricing import implied_yes_prob, kalshi_fee, quotes
from ..storage import Store, utcnow
from ..tuning import Tuner
from .combos import combo_legs, evaluate_combo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CashOutRules:
    lock_multiple: float = 3.0
    risky_below_prob: float = 0.5


def should_cash_out(
    contracts: int, cost: float, bid: float | None, fair_prob: float | None, rules: CashOutRules = CashOutRules()
) -> tuple[bool, str]:
    if contracts <= 0 or not bid:
        return False, "no bid to sell into"
    sale = contracts * bid - kalshi_fee(bid, contracts)
    if fair_prob is not None and sale >= contracts * fair_prob:
        return True, f"selling (${sale:.2f}) beats holding (worth ${contracts * fair_prob:.2f})"
    if (
        cost > 0
        and sale >= cost * rules.lock_multiple
        and fair_prob is not None
        and fair_prob < rules.risky_below_prob
    ):
        return True, (
            f"locks in ${sale:.2f} on ${cost:.2f} staked while remaining legs "
            f"have only {fair_prob:.0%} to hit"
        )
    return False, "holding"


def hold_probability(row, market: dict, lookup, tuner: Tuner, leg_prob=None) -> float | None:
    """The bot's estimate that this bet (on its own side) wins if held to the end."""
    side = row["side"]
    if combo_legs(market):
        kwargs = {"leg_prob": leg_prob} if leg_prob else {}
        p_yes = evaluate_combo(market, lookup, **kwargs).fair_prob
        return None if p_yes is None else (p_yes if side == "yes" else 1 - p_yes)
    mid_yes = implied_yes_prob(market)
    market_p = None if mid_yes is None else (mid_yes if side == "yes" else 1 - mid_yes)
    entry = row["win_prob"]
    if entry is not None:
        entry = tuner.adjust("pick" if row["kind"] == "pick" else "pregame", 1, entry)
    options = [p for p in (entry, market_p) if p is not None]
    return min(options) if options else None


def _cost(row) -> float:
    return row["price"] * row["contracts"] + row["fee"]


class PaperCashOuts:
    """Flag open pretend bets that the cash-out rules would sell right now.

    Called by settle_paper_bets with each open bet's fresh market, so it costs
    no extra API calls. A bet is flagged once; if it later finishes, the
    scorecard compares the sale it would have made with what holding earned.
    """

    TRACKED = ("pick", "pregame", "live")

    def __init__(self, store: Store, lookup, leg_prob=None):
        self.store, self.lookup, self.leg_prob = store, lookup, leg_prob
        self.tuner = Tuner(store)
        self.flagged = 0

    def __call__(self, row, market: dict) -> None:
        if row["kind"] not in self.TRACKED:
            return
        raw = json.loads(row["raw"]) if row["raw"] else {}
        if "cashout" in raw or (row["kind"] == "live" and raw.get("single")):
            return  # already flagged, or a real single bet that live_cash_outs handles
        q = quotes(market)
        bid = q["yes_bid"] if row["side"] == "yes" else q["no_bid"]
        fair = hold_probability(row, market, self.lookup, self.tuner, self.leg_prob)
        sell, reason = should_cash_out(row["contracts"], _cost(row), bid, fair)
        if not sell:
            return
        proceeds = round(row["contracts"] * bid - kalshi_fee(bid, row["contracts"]), 2)
        raw["cashout"] = {"at": utcnow(), "bid": bid, "proceeds": proceeds, "reason": reason}
        with self.store.conn:
            self.store.conn.execute("UPDATE paper_bets SET raw = ? WHERE id = ?", (json.dumps(raw), row["id"]))
        self.store.add_decision(
            {
                "kind": "cashout", "ticker": row["ticker"], "side": row["side"], "action": "would_sell",
                "win_prob": fair, "price": bid, "contracts": row["contracts"], "dry_run": True, "reason": reason,
            }
        )
        self.flagged += 1
        log.info("cash-out flagged %s %s: %s", row["kind"], row["ticker"], reason)


def live_cash_outs(settings: Settings, client: KalshiClient, store: Store, lookup, leg_prob=None,
                   max_positions: int = 10) -> dict:
    """Sell real single-market bets the bot placed when the rules say selling beats holding."""
    stats = {"checked": 0, "sold": 0}
    if not live_enabled(settings):
        return stats
    tuner = Tuner(store)
    rows = store.conn.execute("SELECT * FROM paper_bets WHERE kind = 'live' AND status = 'open'").fetchall()
    for row in rows:
        raw = json.loads(row["raw"]) if row["raw"] else {}
        if not raw.get("single") or stats["checked"] >= max_positions:
            continue
        try:
            market = client.get_market(row["ticker"])
        except KalshiError as exc:
            log.warning("cash-out check %s: %s", row["ticker"], exc)
            continue
        stats["checked"] += 1
        q = quotes(market)
        bid = q["yes_bid"] if row["side"] == "yes" else q["no_bid"]
        fair = hold_probability(row, market, lookup, tuner, leg_prob)
        sell, reason = should_cash_out(row["contracts"], _cost(row), bid, fair)
        if not sell:
            continue
        try:
            result = sell_live_position(settings, client, store, row, bid, reason)
        except KalshiError as exc:
            log.warning("LIVE cash-out %s failed: %s", row["ticker"], exc)
            continue
        stats["sold"] += result["status"] == "sold"
    return stats

"""One pass of the betting logic: evaluate combos, size bets, check cash-outs.

Every decision is written to the `decisions` table. This module never places
orders; order placement arrives in Phase 4 behind DRY_RUN.
"""

import logging
from datetime import datetime, timedelta, timezone

from ..bankroll import BankrollRules, contracts_for, size_bet
from ..config import Settings
from ..kalshi_client import KalshiClient, KalshiError
from ..pricing import implied_yes_prob, quotes
from ..storage import Store
from .cashout import should_cash_out
from .combos import combo_legs, edge_at, evaluate_combo
from .quotes import request_combo_price

log = logging.getLogger(__name__)


def bankroll_rules(settings: Settings) -> BankrollRules:
    return BankrollRules(
        min_stake=settings.min_stake,
        start_max_stake=settings.start_max_stake,
        scale_fraction=settings.scale_fraction,
        standard_cap=settings.max_bet_default,
        high_confidence_prob=settings.high_confidence_prob,
        approval_above=settings.manual_approval_above,
    )


class LegLookup:
    """Find leg markets in the local database first, then the API (with a per-cycle limit)."""

    def __init__(self, client: KalshiClient, store: Store, max_api_calls: int):
        self.client, self.store = client, store
        self.max_api_calls = max_api_calls
        self.api_calls = 0
        self.cache: dict[str, dict | None] = {}

    def __call__(self, ticker: str) -> dict | None:
        if ticker not in self.cache:
            market = self.store.get_market_raw(ticker)
            if market is None and self.api_calls < self.max_api_calls:
                self.api_calls += 1
                try:
                    market = self.client.get_market(ticker)
                except KalshiError as exc:
                    log.debug("leg %s: %s", ticker, exc)
            self.cache[ticker] = market
        return self.cache[ticker]


def _num(d: dict, *names: str) -> float | None:
    for name in names:
        value = d.get(name)
        if value not in (None, ""):
            return float(value) / (1 if name.endswith("_dollars") else 100)
    return None


def evaluate_combos(
    settings: Settings, client: KalshiClient, store: Store, balance: float | None, lifetime_profit: float
) -> dict:
    rules = bankroll_rules(settings)
    lookup = LegLookup(client, store, settings.max_leg_lookups)
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    stats = {"combos_evaluated": 0, "combos_priced": 0, "price_checks": 0, "quotes_received": 0,
             "would_buy": 0, "needs_approval": 0}

    # 1. Fair value for each combo from its legs.
    evaluated = []
    for market in store.combo_markets(settings.combos_to_evaluate):
        ev = evaluate_combo(market, lookup)
        stats["combos_evaluated"] += 1
        if ev.fair_prob is not None and ev.legs_settled < len(ev.legs):
            stats["combos_priced"] += 1
            evaluated.append(ev)

    # 2. Combos have no listed price, so ask market makers for a few quotes.
    can_check = settings.has_credentials and settings.rfq_checks_per_cycle > 0
    for ev in sorted(evaluated, key=lambda e: -e.fair_prob):
        if ev.ask is not None:
            continue
        if not can_check or stats["price_checks"] >= settings.rfq_checks_per_cycle:
            break
        if store.recent_decision(ev.ticker, "price_check", day_ago):
            continue
        try:
            best = request_combo_price(client, ev.ticker, settings.start_max_stake)
        except KalshiError as exc:
            # 409: a request is already open on this combo (e.g. another run of
            # the bot); move on to the next one without using up the check.
            if "-> 409" not in str(exc):
                stats["price_checks"] += 1
            log.warning("price check %s: %s", ev.ticker, exc)
            continue
        stats["price_checks"] += 1
        ev.ask = best[0] if best else None
        ev.edge = edge_at(ev.fair_prob, ev.ask) if ev.ask else None
        stats["quotes_received"] += int(best is not None)
        store.add_decision(
            {
                "kind": "combo",
                "ticker": ev.ticker,
                "side": "yes",
                "action": "price_check",
                "win_prob": ev.fair_prob,
                "price": ev.ask,
                "edge": ev.edge,
                "dry_run": settings.dry_run,
                "reason": "no quotes" if best is None else f"best quote of {len(ev.legs)}-leg combo",
                "raw": {"legs": ev.legs, "quote": best[1] if best else None},
            }
        )
        log.info(
            "price check %s: fair %.2f%%, best quote %s",
            ev.ticker, ev.fair_prob * 100, "none" if ev.ask is None else f"{ev.ask * 100:.2f}%",
        )

    # 3. Size anything with enough edge.
    for ev in evaluated:
        if ev.edge is None or ev.edge < settings.min_edge:
            continue
        stake = size_bet(rules, ev.fair_prob, ev.edge, lifetime_profit, balance)
        if stake.amount <= 0:
            log.info("skip %s: %s", ev.ticker, stake.reason)
            continue
        action = "needs_approval" if stake.needs_approval else "would_buy"
        if store.recent_decision(ev.ticker, action, day_ago):
            continue
        stats[action] += 1
        store.add_decision(
            {
                "kind": "combo",
                "ticker": ev.ticker,
                "side": "yes",
                "action": action,
                "win_prob": ev.fair_prob,
                "price": ev.ask,
                "edge": ev.edge,
                "stake": stake.amount,
                "contracts": contracts_for(stake.amount, ev.ask),
                "dry_run": settings.dry_run,
                "reason": f"{len(ev.legs)} legs, pays {ev.payout_multiple:.1f}x, {stake.reason}",
                "raw": {"legs": ev.legs, "bid": ev.bid},
            }
        )
        log.info(
            "%s %s: %d legs, fair %.2f%% vs ask %.2f%%, edge %.0f%%, stake $%.2f",
            action, ev.ticker, len(ev.legs), ev.fair_prob * 100, ev.ask * 100, ev.edge * 100, stake.amount,
        )
    stats["leg_lookups"] = lookup.api_calls
    return stats


def check_cash_outs(settings: Settings, client: KalshiClient, store: Store) -> dict:
    stats = {"positions": 0, "would_sell": 0}
    if not settings.has_credentials:
        return stats
    positions = client.get_positions().get("market_positions", [])
    lookup = LegLookup(client, store, settings.max_leg_lookups)
    for pos in positions:
        contracts = int(float(pos.get("position_fp") or pos.get("position") or 0))
        if contracts == 0:
            continue
        stats["positions"] += 1
        side = "yes" if contracts > 0 else "no"
        contracts = abs(contracts)
        try:
            market = client.get_market(pos["ticker"])
        except KalshiError as exc:
            log.warning("position %s: %s", pos.get("ticker"), exc)
            continue
        q = quotes(market)
        bid = q["yes_bid"] if side == "yes" else q["no_bid"]
        if combo_legs(market):
            p_yes = evaluate_combo(market, lookup).fair_prob
        else:
            p_yes = implied_yes_prob(market)
        fair = None if p_yes is None else (p_yes if side == "yes" else 1 - p_yes)
        cost = _num(pos, "market_exposure_dollars", "market_exposure") or 0.0
        sell, reason = should_cash_out(contracts, cost, bid, fair)
        if sell:
            stats["would_sell"] += 1
            store.add_decision(
                {
                    "kind": "cashout",
                    "ticker": pos["ticker"],
                    "side": side,
                    "action": "would_sell",
                    "win_prob": fair,
                    "price": bid,
                    "contracts": contracts,
                    "dry_run": settings.dry_run,
                    "reason": reason,
                }
            )
            log.info("would_sell %s: %s", pos["ticker"], reason)
    return stats

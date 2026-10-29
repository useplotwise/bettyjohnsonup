"""Bet sizing rules.

- Start small: every bet is between MIN_STAKE and START_MAX_STAKE ($2 to $5).
- Grow with profit: the max stake rises by SCALE_FRACTION of lifetime net profit,
  up to the $20 standard cap. With no profit yet, bets stay at $2 to $5.
- Above $20 only when the win probability is at least 85% and the stake is no
  more than half the lifetime profit buffer, so a loss can't wipe it out.
- Over $500 never goes through automatically; it's queued for your approval.
- A total allocation can be split across several smaller tickets.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BankrollRules:
    min_stake: float = 2.0
    start_max_stake: float = 5.0
    scale_fraction: float = 0.10
    standard_cap: float = 20.0
    high_confidence_prob: float = 0.85
    buffer_risk_fraction: float = 0.5
    approval_above: float = 500.0


@dataclass(frozen=True)
class Stake:
    amount: float
    needs_approval: bool
    reason: str


def max_stake(rules: BankrollRules, lifetime_profit: float) -> float:
    grown = rules.start_max_stake + rules.scale_fraction * max(0.0, lifetime_profit)
    return round(min(grown, rules.standard_cap), 2)


def size_bet(
    rules: BankrollRules, win_prob: float, edge: float, lifetime_profit: float, balance: float | None
) -> Stake:
    """Pick a stake for a bet with the given win probability and edge (profit per $1)."""
    cap = max_stake(rules, lifetime_profit)
    # Bigger edges get closer to the cap; a 20%+ edge gets the full cap.
    amount = rules.min_stake + (cap - rules.min_stake) * min(1.0, max(0.0, edge) / 0.20)
    reason = f"edge {edge:.0%}, cap ${cap:.2f}"

    buffer = rules.buffer_risk_fraction * max(0.0, lifetime_profit)
    if win_prob >= rules.high_confidence_prob and buffer > rules.standard_cap:
        # Same edge scaling, applied to SCALE_FRACTION of lifetime profit, never above the buffer.
        high = rules.scale_fraction * lifetime_profit * min(1.0, max(0.0, edge) / 0.20)
        amount = max(amount, min(buffer, high))
        reason += f", high confidence ({win_prob:.0%}) allows up to ${buffer:.2f}"

    if balance is not None:
        amount = min(amount, balance)
        if amount < rules.min_stake:
            return Stake(0.0, False, f"balance ${balance:.2f} is below the ${rules.min_stake:.2f} minimum")

    amount = round(amount, 2)
    return Stake(amount, amount > rules.approval_above, reason)


def split_allocation(total: float, tickets: int, min_ticket: float = 1.0) -> list[float]:
    """Split a total across up to `tickets` bets of at least `min_ticket` each."""
    tickets = max(1, min(tickets, int(total // min_ticket)))
    base = round(total / tickets, 2)
    parts = [base] * tickets
    parts[-1] = round(total - base * (tickets - 1), 2)
    return parts


def contracts_for(stake: float, price: float) -> int:
    """Whole contracts a stake buys at a price, fee not included."""
    return math.floor(stake / price + 1e-9) if price > 0 else 0

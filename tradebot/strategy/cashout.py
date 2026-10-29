"""Decide when to sell ("cash out") a combo position before it settles.

Kalshi has no sportsbook-style cash-out offer; the equivalent is selling the
contracts at the current bid. Two rules, either one triggers a sale:

1. EV preservation: selling now is worth at least as much as holding
   (bid after fees >= our estimated chance of the remaining legs hitting).
2. Lock in profit: the sale returns at least LOCK_MULTIPLE x what we paid,
   and the remaining legs are more likely to miss than hit.
"""

from dataclasses import dataclass

from ..pricing import kalshi_fee


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

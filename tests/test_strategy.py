import pytest

from tradebot.bankroll import BankrollRules, contracts_for, max_stake, size_bet, split_allocation
from tradebot.pricing import implied_yes_prob, kalshi_fee, quotes
from tradebot.strategy.cashout import should_cash_out
from tradebot.strategy.combos import combo_legs, evaluate_combo

RULES = BankrollRules()


# -- pricing -------------------------------------------------------------------

def test_quotes_read_dollar_strings_and_imply_missing_asks():
    q = quotes({"yes_bid_dollars": "0.4000", "no_bid_dollars": "0.5500"})
    assert q["yes_bid"] == 0.40 and q["yes_ask"] == 0.45 and q["no_ask"] == 0.60


def test_quotes_read_cents_and_ignore_zero():
    q = quotes({"yes_bid": 30, "yes_ask": 0})
    assert q["yes_bid"] == 0.30 and q["yes_ask"] is None


def test_implied_prob_uses_settled_result_first():
    assert implied_yes_prob({"result": "yes", "yes_bid": 10, "yes_ask": 20}) == 1.0
    assert implied_yes_prob({"yes_bid": 40, "yes_ask": 50}) == pytest.approx(0.45)


def test_fee_rounds_up():
    assert kalshi_fee(0.50) == 0.02  # 0.07 * 0.25 = 0.0175 -> 0.02
    assert kalshi_fee(0.02) == 0.01


# -- combos --------------------------------------------------------------------

COMBO = {
    "ticker": "KXMVE-1",
    "yes_ask_dollars": "0.0500",
    "yes_bid_dollars": "0.0300",
    "custom_strike": {
        "Associated Markets": "LEG-A,LEG-B,LEG-C",
        "Associated Market Sides": "yes,yes,no",
    },
}


def test_combo_legs_from_custom_strike():
    assert combo_legs(COMBO) == [("LEG-A", "yes"), ("LEG-B", "yes"), ("LEG-C", "no")]


def test_combo_legs_from_selected_legs():
    m = {"mve_selected_legs": [{"market_ticker": "X", "side": "no"}]}
    assert combo_legs(m) == [("X", "no")]


def test_evaluate_combo_multiplies_leg_probabilities():
    legs = {
        "LEG-A": {"yes_bid": 49, "yes_ask": 51},  # 0.50
        "LEG-B": {"yes_bid": 39, "yes_ask": 41},  # 0.40
        "LEG-C": {"yes_bid": 69, "yes_ask": 71},  # P(no) = 0.30
    }
    ev = evaluate_combo(COMBO, legs.get)
    assert ev.fair_prob == pytest.approx(0.06)
    assert ev.ask == 0.05
    # (0.06 - 0.05 - 0.01 fee) / 0.05 = 0
    assert ev.edge == pytest.approx(0.0)
    assert ev.payout_multiple == pytest.approx(20)


def test_evaluate_combo_with_missing_leg_has_no_edge():
    ev = evaluate_combo(COMBO, {"LEG-A": {"yes_bid": 49, "yes_ask": 51}}.get)
    assert ev.fair_prob is None and ev.edge is None
    assert ev.missing_legs == ["LEG-B", "LEG-C"]


def test_settled_legs_count_as_certain():
    legs = {"LEG-A": {"result": "yes"}, "LEG-B": {"result": "yes"}, "LEG-C": {"result": "no"}}
    ev = evaluate_combo(COMBO, legs.get)
    assert ev.fair_prob == 1.0 and ev.legs_settled == 3


# -- bankroll ------------------------------------------------------------------

def test_starting_bets_stay_between_2_and_5():
    small = size_bet(RULES, 0.05, 0.05, lifetime_profit=0, balance=100)
    big = size_bet(RULES, 0.05, 0.90, lifetime_profit=0, balance=100)
    assert 2 <= small.amount <= 5 and big.amount == 5


def test_max_stake_grows_with_profit_and_caps_at_20():
    assert max_stake(RULES, 0) == 5
    assert max_stake(RULES, 50) == 10
    assert max_stake(RULES, 1000) == 20


def test_over_20_only_with_high_confidence_and_profit_buffer():
    assert size_bet(RULES, 0.60, 0.5, lifetime_profit=1000, balance=10000).amount == 20
    hi = size_bet(RULES, 0.90, 0.5, lifetime_profit=1000, balance=10000)
    assert 20 < hi.amount <= 500  # at most half the $1000 buffer
    assert size_bet(RULES, 0.90, 0.5, lifetime_profit=0, balance=10000).amount <= 5


def test_over_500_needs_approval():
    stake = size_bet(RULES, 0.95, 0.5, lifetime_profit=10_000, balance=100_000)
    assert stake.amount > 500 and stake.needs_approval


def test_low_balance_skips_bet():
    assert size_bet(RULES, 0.05, 0.5, 0, balance=1.50).amount == 0


def test_split_allocation():
    assert split_allocation(20, 8) == [2.5] * 8
    assert sum(split_allocation(10, 3)) == pytest.approx(10)
    assert split_allocation(3, 8) == [1.0, 1.0, 1.0]


def test_contracts_for():
    assert contracts_for(5, 0.05) == 100
    assert contracts_for(2, 0.30) == 6


# -- cash-outs -----------------------------------------------------------------

def test_cash_out_when_selling_beats_holding():
    sell, reason = should_cash_out(100, cost=5, bid=0.60, fair_prob=0.50)
    assert sell and "beats holding" in reason


def test_cash_out_locks_profit_when_remaining_legs_risky():
    # Spec example: $5,042 potential, $3,523 offer, deteriorating odds.
    sell, reason = should_cash_out(5042, cost=25, bid=0.70, fair_prob=0.45)
    assert sell


def test_hold_when_legs_likely_and_bid_low():
    sell, _ = should_cash_out(100, cost=5, bid=0.50, fair_prob=0.80)
    assert not sell


def test_no_bid_means_hold():
    assert should_cash_out(100, 5, None, 0.1) == (False, "no bid to sell into")

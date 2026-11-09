from datetime import datetime, timedelta, timezone

import pytest

from tradebot.config import Settings
from tradebot.scorecard import write_scorecard
from tradebot.storage import Store
from tradebot.strategy.combos import evaluate_combo
from tradebot.strategy.runner import evaluate_combos
from tradebot.tuning import Tuner

NOW = datetime.now(timezone.utc)


def _bet(store, kind, won, prob, legs=2, pnl=None, days_ago=1):
    when = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    raw = '{"legs": [' + ",".join(['["X", "yes"]'] * legs) + "]}" if kind == "pick" else "{}"
    store.conn.execute(
        "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee, win_prob, status, pnl, "
        "settled_at, raw) VALUES (?, ?, 'T', 'yes', 0.5, 2, 0.02, ?, ?, ?, ?, ?)",
        (when, kind, prob, "won" if won else "lost", pnl if pnl is not None else (0.9 if won else -1.02), when, raw),
    )


def test_overconfident_guesses_are_scaled_down_and_never_up():
    store = Store(":memory:")
    for i in range(100):  # said 60%, won 30%
        _bet(store, "pick", won=i < 30, prob=0.6, legs=2)
    for i in range(100):  # said 20%, won 40%: underconfident, left alone
        _bet(store, "pick", won=i < 40, prob=0.2, legs=5)
    tuner = Tuner(store)
    # ratio 0.5, weight 100/130 -> 1 - 0.5 * 0.769 = 0.615
    assert tuner.factor("pick", 2) == pytest.approx(0.6154, abs=1e-3)
    assert tuner.adjust("pick", 2, 0.6) == pytest.approx(0.3692, abs=1e-3)
    assert tuner.factor("pick", 5) == 1.0
    assert tuner.factor("pick", 3) == 1.0  # no data for this group


def test_small_samples_barely_move_the_guess():
    store = Store(":memory:")
    for _ in range(3):  # three straight losses
        _bet(store, "pregame", won=False, prob=0.5)
    assert Tuner(store).factor("pregame", 1) > 0.9


def test_losing_groups_pause_and_old_results_expire():
    store = Store(":memory:")
    for i in range(30):
        _bet(store, "pick", won=i < 10, prob=0.4, legs=3, pnl=1.0 if i < 10 else -1.0)
    assert Tuner(store).paused("pick", 4)  # 3-4 legs share a group
    assert not Tuner(store).paused("pick", 2)
    assert not Tuner(store, now=NOW + timedelta(days=30)).paused("pick", 3)  # losses aged out
    store2 = Store(":memory:")
    for i in range(29):  # one short of the minimum
        _bet(store2, "pick", won=False, prob=0.4, legs=3)
    assert not Tuner(store2).paused("pick", 3)


def test_same_game_legs_use_the_cautious_bound():
    combo = {"ticker": "C", "yes_ask_dollars": "0.30",
             "custom_strike": {"Associated Markets": "A,B", "Associated Market Sides": "yes,yes"}}
    same = {"A": {"event_ticker": "G1", "yes_bid": 69, "yes_ask": 71},
            "B": {"event_ticker": "G1", "yes_bid": 59, "yes_ask": 61}}
    ev = evaluate_combo(combo, same.get)
    assert ev.fair_prob == pytest.approx(0.30)  # 0.70 + 0.60 - 1, below the 0.42 product
    assert ev.same_game_legs == 2
    different = {"A": {**same["A"], "event_ticker": "G1"}, "B": {**same["B"], "event_ticker": "G2"}}
    ev = evaluate_combo(combo, different.get)
    assert ev.fair_prob == pytest.approx(0.42) and ev.same_game_legs == 0


COMBO = {
    "ticker": "KXMVE-GOOD", "status": "active", "yes_ask_dollars": "0.0400",
    "custom_strike": {"Associated Markets": "A,B", "Associated Market Sides": "yes,yes"},
}
LEGS = {"A": {"ticker": "A", "yes_bid": 29, "yes_ask": 31}, "B": {"ticker": "B", "yes_bid": 19, "yes_ask": 21}}


class FakeClient:
    def get_market(self, ticker):
        return LEGS[ticker]


def _store_with_combo():
    store = Store(":memory:")
    store.upsert_markets([{"ticker": "KXMVE-GOOD", "event_ticker": None, "series_ticker": None, "category": None,
                           "title": None, "status": "active", "is_combo": 1, "close_time": None, "result": None,
                           "raw": COMBO}])
    return store


def test_overconfident_history_removes_marginal_picks_and_pause_blocks_new_ones(tmp_path):
    settings = Settings(log_dir=tmp_path)
    # fair 0.06 vs ask 0.04: edge about 0.25 raw.
    store = _store_with_combo()
    for _ in range(100):  # picks said 60%, won 0%: guesses scaled to 0.25 of the original
        _bet(store, "pick", won=False, prob=0.6, legs=2, pnl=1.0)  # profitable, so not paused
    stats = evaluate_combos(settings, FakeClient(), store, 50, 0)
    assert stats["would_buy"] == 0 and stats["tuned_out"] == 1

    store = _store_with_combo()
    for i in range(30):  # losing group (net loss) whose guesses were right: paused, not scaled
        _bet(store, "pick", won=i < 2, prob=0.06, legs=2, pnl=-1.0)
    stats = evaluate_combos(settings, FakeClient(), store, 50, 0)
    assert stats["would_buy"] == 0 and stats["paused"] == 1


def test_picks_keep_the_raw_guess_for_later_tuning(tmp_path):
    store = _store_with_combo()
    evaluate_combos(Settings(log_dir=tmp_path), FakeClient(), store, 50, 0)
    [bet] = store.conn.execute("SELECT * FROM paper_bets WHERE kind = 'pick'").fetchall()
    assert bet["win_prob"] == pytest.approx(0.06)
    text = write_scorecard(store, tmp_path, 50, True).read_text()
    assert "Self-tuning" in text

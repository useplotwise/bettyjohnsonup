import json
from dataclasses import replace

import pytest

from tradebot.config import Settings
from tradebot.kalshi_client import KalshiClient
from tradebot.live import bot_ledger
from tradebot.paper import settle_paper_bets
from tradebot.scorecard import write_scorecard
from tradebot.storage import Store, utcnow
from tradebot.strategy.cashout import PaperCashOuts, hold_probability, live_cash_outs
from tradebot.tuning import Tuner

LIVE = Settings(dry_run=False, bot_bankroll=20, kalshi_api_key_id="k", kalshi_private_key_path="p",
                builds_per_cycle=0, rfq_checks_per_cycle=0)
NOTHING = lambda ticker: None  # noqa: E731


def _bet(store, kind="pick", ticker="S", side="yes", price=0.40, contracts=10, fee=0.17, win_prob=0.55, raw=None):
    store.conn.execute(
        "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee, win_prob, edge, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.2, ?)",
        (utcnow(), kind, ticker, side, price, contracts, fee, win_prob, json.dumps(raw or {})))
    store.conn.commit()
    return store.conn.execute("SELECT * FROM paper_bets ORDER BY id DESC LIMIT 1").fetchone()


class Market:
    """Fake Kalshi that serves one market and records orders."""

    def __init__(self, bid=62, ask=64, result=None, fill=None):
        self.bid, self.ask, self.result, self.fill = bid, ask, result, fill
        self.orders, self.cancelled = [], []

    def get_market(self, ticker):
        m = {"ticker": ticker, "status": "settled" if self.result else "active",
             "yes_bid": self.bid, "yes_ask": self.ask}
        if self.result:
            m["result"] = self.result
        return m

    def create_order(self, ticker, side, count, price, client_order_id, action="buy"):
        self.orders.append((ticker, side, count, price, action))
        return {"order_id": "o1"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def get_fills(self, **params):
        ticker, side, count, price, action = self.orders[-1]
        n = count if self.fill is None else self.fill
        return {"fills": [{"side": side, "action": action, "count": n, f"{side}_price": round(price * 100)}]
                if n else []}


def test_hold_probability_for_singles_is_the_lower_of_estimate_and_market():
    store = Store(":memory:")
    tuner = Tuner(store)
    row = _bet(store, win_prob=0.60)
    assert hold_probability(row, {"yes_bid": 49, "yes_ask": 51}, NOTHING, tuner) == pytest.approx(0.50)
    assert hold_probability(row, {"yes_bid": 79, "yes_ask": 81}, NOTHING, tuner) == pytest.approx(0.60)
    no_row = _bet(store, side="no", win_prob=0.30)
    assert hold_probability(no_row, {"yes_bid": 49, "yes_ask": 51}, NOTHING, tuner) == pytest.approx(0.30)


def test_hold_probability_for_combos_uses_current_legs():
    store = Store(":memory:")
    combo = {"ticker": "C", "custom_strike": {"Associated Markets": "A,B", "Associated Market Sides": "yes,yes"}}
    legs = {"A": {"yes_bid": 49, "yes_ask": 51}, "B": {"result": "no"}}
    row = _bet(store, ticker="C")
    assert hold_probability(row, combo, legs.get, Tuner(store)) == 0.0  # a leg already lost


def test_pretend_cash_out_is_flagged_once_and_compared_with_holding(tmp_path):
    store = Store(":memory:")
    _bet(store)  # bought 10 YES at 0.40, our estimate 0.55
    client = Market(bid=62, ask=64)
    on_open = PaperCashOuts(store, NOTHING)
    settle_paper_bets(client, store, on_open=on_open)
    settle_paper_bets(client, store, on_open=on_open)
    assert on_open.flagged == 1
    [d] = [r for r in store.decisions_since("2000-01-01") if r["action"] == "would_sell"]
    assert d["kind"] == "cashout" and d["dry_run"] == 1 and d["price"] == 0.62
    # The bet is still open and keeps settling normally; the sale would have beaten holding.
    client.result = "no"
    settle_paper_bets(client, store, on_open=on_open)
    text = write_scorecard(store, tmp_path, 50, True).read_text()
    assert "1 bets flagged for selling early" in text
    assert "selling when flagged would have made $+1.86" in text  # 6.20 - 0.17 sale fee - (4.00 + 0.17 fee) cost
    assert "holding made $-4.17" in text


def test_no_pretend_cash_out_when_holding_is_better():
    store = Store(":memory:")
    _bet(store, win_prob=0.80)
    on_open = PaperCashOuts(store, NOTHING)
    settle_paper_bets(Market(bid=62, ask=64), store, on_open=on_open)
    assert on_open.flagged == 0  # sale 6.03 < 10 * min(0.80, 0.63)


def test_live_cash_out_sells_the_bots_own_single_bet(monkeypatch):
    monkeypatch.setattr("tradebot.live.time.sleep", lambda s: None)
    store, client = Store(":memory:"), Market(bid=62)
    row = _bet(store, kind="live", raw={"single": True})
    stats = live_cash_outs(LIVE, client, store, NOTHING)
    assert stats == {"checked": 1, "sold": 1}
    assert client.orders == [("S", "yes", 10, 0.62, "sell")] and client.cancelled == ["o1"]
    bet = store.conn.execute("SELECT * FROM paper_bets WHERE id = ?", (row["id"],)).fetchone()
    assert bet["status"] == "sold" and bet["pnl"] == pytest.approx(6.20 - 0.17 - 4.17, abs=0.01)
    assert bot_ledger(store)["profit"] == bet["pnl"] and bot_ledger(store)["open"] == 0
    assert any(d["action"] == "sold" for d in store.decisions_since("2000-01-01"))


def test_partial_sale_splits_the_bet(monkeypatch):
    monkeypatch.setattr("tradebot.live.time.sleep", lambda s: None)
    store, client = Store(":memory:"), Market(bid=62, fill=4)
    row = _bet(store, kind="live", raw={"single": True})
    live_cash_outs(LIVE, client, store, NOTHING)
    rows = {r["status"]: r for r in store.conn.execute("SELECT * FROM paper_bets")}
    assert rows["open"]["contracts"] == 6 and rows["sold"]["contracts"] == 4
    assert rows["open"]["fee"] + rows["sold"]["fee"] == pytest.approx(0.17)
    assert rows["open"]["id"] == row["id"]


def test_unfilled_sale_leaves_the_bet_open(monkeypatch):
    monkeypatch.setattr("tradebot.live.time.sleep", lambda s: None)
    store, client = Store(":memory:"), Market(bid=62, fill=0)
    _bet(store, kind="live", raw={"single": True})
    assert live_cash_outs(LIVE, client, store, NOTHING)["sold"] == 0
    assert bot_ledger(store)["open"] == 1
    assert any(d["action"] == "sell_skipped" for d in store.decisions_since("2000-01-01"))


def test_live_cash_outs_only_touch_bot_singles_and_only_when_live():
    store, client = Store(":memory:"), Market(bid=62)
    _bet(store, kind="live", raw={"legs": [["A", "yes"]]})  # a combo: no order book to sell into
    _bet(store, kind="pick")  # a pretend bet
    assert live_cash_outs(LIVE, client, store, NOTHING) == {"checked": 0, "sold": 0}
    _bet(store, kind="live", raw={"single": True})
    assert live_cash_outs(replace(LIVE, dry_run=True), client, store, NOTHING) == {"checked": 0, "sold": 0}
    assert client.orders == []


def test_sell_order_body():
    client = KalshiClient("http://example.test")
    sent = {}
    client.request = lambda method, path, json=None, signed=False: sent.update(body=json) or {"order": {}}
    client.create_order("T", "no", 3, 0.41, "id1", action="sell")
    assert sent["body"] == {"ticker": "T", "client_order_id": "id1", "action": "sell", "side": "no",
                            "count": 3, "type": "limit", "no_price": 41}

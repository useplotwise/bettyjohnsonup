from dataclasses import replace

from tradebot.config import Settings
from tradebot.live import bot_ledger, live_enabled, money_available, place_live_bet
from tradebot.paper import settle_paper_bets
from tradebot.scorecard import write_scorecard
from tradebot.storage import Store
from tradebot.strategy.runner import evaluate_combos

LIVE = Settings(dry_run=False, bot_bankroll=20, kalshi_api_key_id="k", kalshi_private_key_path="p",
                builds_per_cycle=0, rfq_checks_per_cycle=0, live_max_stake=20)
NO_SLEEP = lambda s: None  # noqa: E731


class MarketMaker:
    """Fake Kalshi: quotes YES at `price`, confirms (or not) when accepted."""

    def __init__(self, price="0.3000", confirm=True, fill_price="0.2900"):
        self.price, self.confirm, self.fill_price = price, confirm, fill_price
        self.accepted, self.deleted, self.rfqs = [], [], []

    def create_rfq(self, ticker, target):
        self.rfqs.append((ticker, target))
        return "rfq1"

    def get_rfq_quotes(self, rfq_id):
        return [{"id": "q1", "status": "open", "yes_bid_dollars": self.price, "no_bid_dollars": "0"}]

    def accept_quote(self, rfq_id, quote_id, side):
        self.accepted.append((rfq_id, quote_id, side))

    def get_quote(self, rfq_id, quote_id):
        status = "executed" if self.confirm else "accepted"
        return {"id": quote_id, "status": status, "yes_contracts_fp": "16.00"}

    def delete_rfq(self, rfq_id):
        self.deleted.append(rfq_id)

    def get_fills(self, **params):
        return {"fills": [{"side": "yes", "action": "buy", "count_fp": "16.00", "yes_price_dollars": self.fill_price}]}

    def get_market(self, ticker):
        return {"ticker": ticker, "status": "settled", "result": "yes"}


def test_live_needs_both_switches():
    assert not live_enabled(Settings())
    assert not live_enabled(replace(LIVE, bot_bankroll=0))
    assert not live_enabled(replace(LIVE, dry_run=True))
    assert live_enabled(LIVE)


def test_buys_at_a_good_fresh_price_and_records_the_fill():
    store, mm = Store(":memory:"), MarketMaker()
    result = place_live_bet(LIVE, mm, store, "KXMVE-A", 0.5, 5.0, [("L1", "yes")], sleep=NO_SLEEP)
    assert result["status"] == "bought" and result["price"] == 0.29 and result["contracts"] == 16
    assert mm.accepted == [("rfq1", "q1", "yes")] and mm.rfqs == [("KXMVE-A", 5.0)] and mm.deleted
    ledger = bot_ledger(store)
    assert ledger["open"] == 1 and ledger["bets_today"] == 1
    assert money_available(LIVE, store) == round(20 - (16 * 0.29 + 0.24), 2)
    [d] = store.decisions_since("2000-01-01")
    assert d["action"] == "bought" and d["dry_run"] == 0

    settle_paper_bets(mm, store)
    assert bot_ledger(store)["profit"] == round(16 - 16 * 0.29 - 0.24, 2)


def test_no_trade_when_fresh_price_has_no_edge():
    store, mm = Store(":memory:"), MarketMaker(price="0.4900")
    result = place_live_bet(LIVE, mm, store, "KXMVE-A", 0.5, 5.0, sleep=NO_SLEEP)
    assert result["status"] == "skipped" and "edge" in result["reason"]
    assert mm.accepted == [] and mm.deleted == ["rfq1"]
    assert bot_ledger(store)["open"] == 0


def test_unconfirmed_trade_is_not_recorded_as_a_bet():
    store, mm = Store(":memory:"), MarketMaker(confirm=False)
    result = place_live_bet(LIVE, mm, store, "KXMVE-A", 0.5, 5.0, sleep=NO_SLEEP)
    assert result["status"] == "skipped" and "did not confirm" in result["reason"]
    assert bot_ledger(store)["open"] == 0


def test_stays_within_bot_money_and_daily_limit():
    store, mm = Store(":memory:"), MarketMaker()
    small = replace(LIVE, bot_bankroll=3)
    result = place_live_bet(small, mm, store, "KXMVE-A", 0.5, 5.0, sleep=NO_SLEEP)
    assert mm.rfqs == [("KXMVE-A", 3.0)] and result["status"] == "bought"
    # $3 is now tied up, so nothing is left for a second bet.
    result = place_live_bet(small, mm, store, "KXMVE-B", 0.5, 5.0, sleep=NO_SLEEP)
    assert result["status"] == "skipped" and "minimum" in result["reason"] and len(mm.rfqs) == 1

    one_a_day = replace(LIVE, max_live_bets_per_day=1)
    result = place_live_bet(one_a_day, mm, store, "KXMVE-C", 0.5, 5.0, sleep=NO_SLEEP)
    assert "daily limit" in result["reason"]


def _store_with_combo():
    combo = {"ticker": "KXMVE-GOOD", "status": "active", "yes_ask_dollars": "0.0400",
             "custom_strike": {"Associated Markets": "A,B", "Associated Market Sides": "yes,yes"}}
    store = Store(":memory:")
    store.upsert_markets(
        [{"ticker": combo["ticker"], "event_ticker": None, "series_ticker": None, "category": None,
          "title": None, "status": "active", "is_combo": 1, "close_time": None, "result": None, "raw": combo}]
    )
    return store


class RunnerClient(MarketMaker):
    def get_market(self, ticker):
        return {"A": {"ticker": "A", "yes_bid": 29, "yes_ask": 31},
                "B": {"ticker": "B", "yes_bid": 19, "yes_ask": 21}}[ticker]


def test_runner_buys_only_when_live(tmp_path, monkeypatch):
    monkeypatch.setattr("tradebot.live.time.sleep", NO_SLEEP)
    client = RunnerClient(price="0.0400", fill_price="0.0400")
    stats = evaluate_combos(replace(LIVE, log_dir=tmp_path), client, _store_with_combo(), 50, 0)
    assert stats["would_buy"] == 1 and stats["bought"] == 1

    client = RunnerClient(price="0.0400")
    stats = evaluate_combos(replace(LIVE, dry_run=True, log_dir=tmp_path), client, _store_with_combo(), 50, 0)
    assert stats["would_buy"] == 1 and stats["bought"] == 0 and client.accepted == []


def test_scorecard_shows_real_bets(tmp_path):
    store = Store(":memory:")
    place_live_bet(LIVE, MarketMaker(), store, "KXMVE-A", 0.5, 5.0, sleep=NO_SLEEP)
    text = write_scorecard(store, tmp_path, 25.0, False, LIVE).read_text()
    assert "LIVE (real bets)" in text and "## Real bets" in text and "Real bets placed today: 1" in text
    text = write_scorecard(store, tmp_path, 25.0, False, replace(LIVE, bot_bankroll=0)).read_text()
    assert "BOT_BANKROLL is 0" in text


class SingleMarketMaker:
    """Fake Kalshi for single-market orders: fills `fill` of the order at the asked price."""

    def __init__(self, yes_ask=40, fill=None):
        self.yes_ask, self.fill = yes_ask, fill
        self.orders, self.cancelled = [], []

    def get_market(self, ticker):
        return {"ticker": ticker, "yes_bid": self.yes_ask - 2, "yes_ask": self.yes_ask}

    def create_order(self, ticker, side, count, price, client_order_id):
        self.orders.append((ticker, side, count, price))
        return {"order_id": "o1"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def get_fills(self, **params):
        ticker, side, count, price = self.orders[-1]
        n = count if self.fill is None else self.fill
        return {"fills": [{"side": side, "action": "buy", "count": n, f"{side}_price": round(price * 100)}]
                if n else []}


def test_single_market_bet_buys_at_fresh_price_and_cancels_rest():
    from tradebot.live import place_live_single

    store, mm = Store(":memory:"), SingleMarketMaker(yes_ask=40, fill=6)
    result = place_live_single(LIVE, mm, store, "KXNBAGAME-X-LAL", "yes", 0.55, 5.0, sleep=NO_SLEEP)
    assert mm.orders == [("KXNBAGAME-X-LAL", "yes", 12, 0.40)] and mm.cancelled == ["o1"]
    assert result["status"] == "bought" and result["contracts"] == 6 and result["price"] == 0.40
    assert bot_ledger(store)["open"] == 1

    # Price moved: no edge left, so no order.
    mm2 = SingleMarketMaker(yes_ask=54)
    result = place_live_single(LIVE, mm2, Store(":memory:"), "T", "yes", 0.55, 5.0, sleep=NO_SLEEP)
    assert result["status"] == "skipped" and mm2.orders == []

    # Nothing filled: no bet recorded.
    store3, mm3 = Store(":memory:"), SingleMarketMaker(yes_ask=40, fill=0)
    result = place_live_single(LIVE, mm3, store3, "T", "yes", 0.55, 5.0, sleep=NO_SLEEP)
    assert result["status"] == "skipped" and "didn't fill" in result["reason"]
    assert bot_ledger(store3)["open"] == 0


def _settled_live(store, pnl, settled_at=None):
    from datetime import datetime, timezone
    now = settled_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee, status, pnl, settled_at) "
        "VALUES (?, 'live', 'T', 'yes', 0.5, 2, 0.02, ?, ?, ?)",
        (now, "won" if pnl > 0 else "lost", pnl, now))
    store.conn.commit()


def test_daily_loss_stop_pauses_for_the_day_only():
    from tradebot.live import real_pnl_today, safety_stop
    store, mm = Store(":memory:"), MarketMaker()
    _settled_live(store, -5.0)  # 25% of the $20 bot money, lost today
    assert real_pnl_today(store) == -5.0 and "daily limit" in safety_stop(LIVE, store)
    result = place_live_bet(LIVE, mm, store, "KXMVE-A", 0.5, 5.0, sleep=NO_SLEEP)
    assert result["status"] == "skipped" and "paused for today" in result["reason"] and mm.rfqs == []
    # The same loss from yesterday doesn't stop today's betting.
    store2 = Store(":memory:")
    _settled_live(store2, -5.0, settled_at="2020-01-01T00:00:00+00:00")
    assert safety_stop(LIVE, store2) == ""


def test_drawdown_stop_and_switches():
    from tradebot.live import safety_stop
    store = Store(":memory:")
    _settled_live(store, -6.0, settled_at="2020-01-01T00:00:00+00:00")
    _settled_live(store, -5.0, settled_at="2020-01-02T00:00:00+00:00")  # -11 total, past 50% of $20
    assert "stopped" in safety_stop(LIVE, store)
    assert safety_stop(replace(LIVE, max_drawdown_fraction=0), store) == ""
    assert safety_stop(replace(LIVE, bot_bankroll=40), store) == ""  # -11 is under 50% of $40


def test_first_live_stage_is_one_dollar_by_default():
    store, mm = Store(":memory:"), MarketMaker()
    one = replace(LIVE, live_max_stake=Settings().live_max_stake)
    assert one.live_max_stake == 1.0
    result = place_live_bet(one, mm, store, "KXMVE-A", 0.5, 5.0, sleep=NO_SLEEP)
    assert mm.rfqs == [("KXMVE-A", 1.0)] and result["stake"] == 1.0


def test_single_bets_stay_pretend_unless_switched_on(tmp_path):
    from tradebot.odds import BookOdds
    from tradebot.strategy.runner import bankroll_rules
    from tradebot.strategy.value import find_value_bets

    calls = []
    monkey_live = lambda *a, **k: calls.append(a) or {"status": "bought"}  # noqa: E731
    import tradebot.strategy.value as value
    original = value.place_live_single
    value.place_live_single = monkey_live
    try:
        settings = replace(LIVE, log_dir=tmp_path)
        assert not settings.live_singles
        book = BookOdds([])
        book.games = ["x"]
        book.yes_prob = lambda m: 0.60
        store = Store(":memory:")
        market = {"ticker": "KXNBAGAME-26NOV12LALLAC-LAL", "event_ticker": "KXNBAGAME-26NOV12LALLAC",
                  "status": "active", "title": "Lakers", "yes_bid_dollars": "0.4800", "yes_ask_dollars": "0.5000"}
        store.upsert_markets([{"ticker": market["ticker"], "event_ticker": market["event_ticker"],
                               "series_ticker": None, "category": None, "title": "Lakers", "status": "active",
                               "is_combo": 0, "close_time": None, "result": None, "raw": market}])
        stats = find_value_bets(settings, store, book, bankroll_rules(settings), 0, client=object())
        assert stats["value_bets"] == 1 and stats["bought"] == 0 and calls == []
    finally:
        value.place_live_single = original

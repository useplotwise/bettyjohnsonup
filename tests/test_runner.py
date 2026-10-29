from dataclasses import replace

from tradebot.config import Settings
from tradebot.kalshi_client import KalshiError
from tradebot.scorecard import write_scorecard
from tradebot.storage import Store
from tradebot.strategy.runner import check_cash_outs, evaluate_combos

COMBO = {
    "ticker": "KXMVE-GOOD",
    "status": "active",
    "yes_ask_dollars": "0.0400",
    "custom_strike": {"Associated Markets": "A,B", "Associated Market Sides": "yes,yes"},
}
LEGS = {"A": {"ticker": "A", "yes_bid": 29, "yes_ask": 31}, "B": {"ticker": "B", "yes_bid": 19, "yes_ask": 21}}


class FakeClient:
    def __init__(self, positions=None, markets=None):
        self.positions = positions or []
        self.markets = markets or {}
        self.market_calls = 0

    def get_market(self, ticker):
        self.market_calls += 1
        return self.markets.get(ticker) or LEGS[ticker]

    def get_positions(self):
        return {"market_positions": self.positions}


def _store_with_combo():
    store = Store(":memory:")
    store.upsert_markets(
        [{"ticker": COMBO["ticker"], "event_ticker": None, "series_ticker": None, "category": None,
          "title": None, "status": "active", "is_combo": 1, "close_time": None, "result": None,
          "raw": COMBO}]
    )
    return store


def test_good_combo_is_recorded_once_per_day(tmp_path):
    store = _store_with_combo()
    settings = Settings(log_dir=tmp_path)
    # fair = 0.30 * 0.20 = 0.06 vs ask 0.04 -> big edge
    stats = evaluate_combos(settings, FakeClient(), store, balance=50, lifetime_profit=0)
    assert stats["would_buy"] == 1
    [row] = store.decisions_since("2000-01-01")
    assert row["action"] == "would_buy" and row["stake"] == 5 and row["dry_run"] == 1
    [bet] = store.conn.execute("SELECT * FROM paper_bets").fetchall()
    assert bet["kind"] == "pick" and bet["price"] == 0.04 and bet["contracts"] == row["contracts"]
    assert evaluate_combos(settings, FakeClient(), store, 50, 0)["would_buy"] == 0

    path = write_scorecard(store, tmp_path, 50, True)
    text = path.read_text()
    assert "KXMVE-GOOD" in text and "DRY RUN" in text


def test_edge_below_minimum_is_skipped(tmp_path):
    store = _store_with_combo()
    settings = Settings(log_dir=tmp_path, min_edge=10.0)
    assert evaluate_combos(settings, FakeClient(), store, 50, 0)["would_buy"] == 0


def test_cash_out_check_records_sale(tmp_path):
    store = Store(":memory:")
    settings = Settings(log_dir=tmp_path, kalshi_api_key_id="k", kalshi_private_key_path="p")
    client = FakeClient(
        positions=[{"ticker": "SINGLE", "position": 10, "market_exposure": 300}],
        markets={"SINGLE": {"ticker": "SINGLE", "yes_bid": 80, "yes_ask": 82}},
    )
    stats = check_cash_outs(settings, client, store)
    # Bid 80c vs a fair 81c and $8 isn't 3x the $3 cost: hold.
    assert stats == {"positions": 1, "would_sell": 0}
    # Bought cheap ($0.50 for 10), now bid 30c with only a 35% chance left: lock it in.
    client.positions = [{"ticker": "SINGLE", "position": 10, "market_exposure": 50}]
    client.markets["SINGLE"] = {"ticker": "SINGLE", "yes_bid": 30, "yes_ask": 40}
    assert check_cash_outs(settings, client, store)["would_sell"] == 1
    [row] = store.decisions_since("2000-01-01")
    assert row["action"] == "would_sell" and "locks in" in row["reason"]


UNPRICED = {**COMBO, "ticker": "KXMVE-UNPRICED", "yes_ask_dollars": None}


class QuotingClient(FakeClient):
    def __init__(self, quotes):
        super().__init__()
        self.quotes = quotes
        self.rfqs = []

    def create_rfq(self, ticker, target_cost):
        self.rfqs.append(ticker)
        return "rfq-1"

    def get_rfq_quotes(self, rfq_id):
        return self.quotes

    def delete_rfq(self, rfq_id):
        pass


def _store_with(market):
    store = Store(":memory:")
    store.upsert_markets(
        [{"ticker": market["ticker"], "event_ticker": None, "series_ticker": None, "category": None,
          "title": None, "status": "active", "is_combo": 1, "close_time": None, "result": None,
          "raw": market}]
    )
    return store


def test_unpriced_combo_gets_a_price_check(tmp_path, monkeypatch):
    monkeypatch.setattr("tradebot.strategy.quotes.time.sleep", lambda s: None)
    store = _store_with(UNPRICED)
    settings = Settings(log_dir=tmp_path, kalshi_api_key_id="k", kalshi_private_key_path="p")
    client = QuotingClient([{"status": "open", "yes_bid_dollars": "0.0400", "no_bid_dollars": "0"}])
    stats = evaluate_combos(settings, client, store, balance=50, lifetime_profit=0)
    assert client.rfqs == ["KXMVE-UNPRICED"]
    assert stats["price_checks"] == 1 and stats["quotes_received"] == 1 and stats["would_buy"] == 1
    actions = sorted(r["action"] for r in store.decisions_since("2000-01-01"))
    assert actions == ["price_check", "would_buy"]
    kinds = sorted(r[0] for r in store.conn.execute("SELECT kind FROM paper_bets"))
    assert kinds == ["pick", "watch"]
    # Not re-checked within a day.
    assert evaluate_combos(settings, client, store, 50, 0)["price_checks"] == 0


def test_no_price_checks_without_credentials(tmp_path):
    store = _store_with(UNPRICED)
    client = QuotingClient([])
    stats = evaluate_combos(Settings(log_dir=tmp_path), client, store, 50, 0)
    assert client.rfqs == [] and stats["combos_priced"] == 1


class BusyThenQuotingClient(QuotingClient):
    def create_rfq(self, ticker, target_cost):
        if not self.rfqs:
            self.rfqs.append(ticker)
            raise KalshiError("POST /communications/rfqs -> 409: already exists")
        return super().create_rfq(ticker, target_cost)


def test_busy_combo_is_skipped_without_using_the_check(tmp_path, monkeypatch):
    monkeypatch.setattr("tradebot.strategy.quotes.time.sleep", lambda s: None)
    store = _store_with(UNPRICED)
    second = {**UNPRICED, "ticker": "KXMVE-SECOND"}
    store.upsert_markets(
        [{"ticker": second["ticker"], "event_ticker": None, "series_ticker": None, "category": None,
          "title": None, "status": "active", "is_combo": 1, "close_time": None, "result": None,
          "raw": second}]
    )
    settings = Settings(log_dir=tmp_path, kalshi_api_key_id="k", kalshi_private_key_path="p")
    client = BusyThenQuotingClient([{"status": "open", "yes_bid_dollars": "0.0400", "no_bid_dollars": "0"}])
    stats = evaluate_combos(settings, client, store, balance=50, lifetime_profit=0)
    assert len(client.rfqs) == 2 and stats["price_checks"] == 1 and stats["quotes_received"] == 1

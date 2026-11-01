import random

from tradebot.config import Settings
from tradebot.scorecard import write_scorecard
from tradebot.paper import settle_paper_bets
from tradebot.storage import Store
from tradebot.strategy.builder import build_combo, candidate_legs, choose_legs
from tradebot.strategy.runner import evaluate_combos


def _market(ticker, event, bid, ask):
    return {"ticker": ticker, "event_ticker": event, "status": "active", "yes_bid": bid, "yes_ask": ask}


MARKETS = [
    _market("A-WIN", "GAME-A", 79, 81),  # yes 80%
    _market("A-TIE", "GAME-A", 9, 11),   # no 90%
    _market("B-WIN", "GAME-B", 69, 71),  # yes 70%
    _market("C-WIN", "GAME-C", 49, 51),  # 50%: out of range
    _market("D-WIN", "GAME-D", 60, 90),  # spread too wide
    _market("E-WIN", "OTHER", 79, 81),   # event not in the collection
    {"ticker": "KXMVE-X", "event_ticker": "GAME-A", "status": "active", "yes_bid": 79, "yes_ask": 81},
]
ELIGIBLE = {"GAME-A", "GAME-B", "GAME-C", "GAME-D"}


def test_candidate_legs_filters_and_takes_either_side():
    legs = candidate_legs(MARKETS, ELIGIBLE, 0.65, 0.95, 0.06)
    assert {(l.market_ticker, l.side, l.prob) for l in legs} == {
        ("A-WIN", "yes", 0.8), ("A-TIE", "no", 0.9), ("B-WIN", "yes", 0.7)
    }
    assert all(l.side == "yes" for l in candidate_legs(MARKETS, ELIGIBLE, 0.65, 0.95, 0.06, yes_only=True))


def test_choose_legs_uses_one_leg_per_event():
    legs = candidate_legs(MARKETS, ELIGIBLE, 0.65, 0.95, 0.06)
    for seed in range(20):
        chosen = choose_legs(legs, 2, random.Random(seed))
        assert len({l.event_ticker for l in chosen}) == 2
    assert choose_legs(legs, 3, random.Random(0)) == []  # only 2 events available


class BuilderClient:
    def __init__(self, quote_yes_bid="0.5000"):
        self.created, self.rfqs = [], []
        self.quote_yes_bid = quote_yes_bid

    def get_collection(self, ticker):
        return {"collection_ticker": ticker, "associated_event_tickers": sorted(ELIGIBLE), "size_min": 2}

    def create_combo(self, collection, legs):
        self.created.append(legs)
        return f"KXMVE-BUILT-{len(self.created)}"

    def create_rfq(self, ticker, target):
        self.rfqs.append(ticker)
        return "rfq1"

    def get_rfq_quotes(self, rfq_id):
        return [{"status": "open", "yes_bid_dollars": self.quote_yes_bid, "no_bid_dollars": "0"}]

    def delete_rfq(self, rfq_id):
        pass

    def get_market(self, ticker):
        return {"ticker": ticker, "status": "settled", "result": "yes"}


def _store():
    store = Store(":memory:")
    store.upsert_markets(
        [{"ticker": m["ticker"], "event_ticker": m["event_ticker"], "series_ticker": None, "category": None,
          "title": None, "status": "active", "is_combo": int(m["ticker"].startswith("KXMVE")),
          "close_time": None, "result": None, "raw": m} for m in MARKETS]
    )
    return store


def _settings(tmp_path, **kw):
    return Settings(log_dir=tmp_path, kalshi_api_key_id="k", kalshi_private_key_path="p",
                    builder_leg_counts=(2,), **kw)


def test_build_creates_prices_and_paper_trades(tmp_path, monkeypatch):
    monkeypatch.setattr("tradebot.strategy.quotes.time.sleep", lambda s: None)
    store, client = _store(), BuilderClient()
    ev = build_combo(_settings(tmp_path), client, store, random.Random(1))
    [legs] = client.created
    assert len(legs) == 2 and {"event_ticker", "market_ticker", "side"} <= set(legs[0])
    assert ev.ticker == "KXMVE-BUILT-1" and ev.ask == 0.5
    assert 0.55 < ev.fair_prob < 0.73  # 0.8 or 0.9 times 0.7
    [bet] = store.conn.execute("SELECT * FROM paper_bets").fetchall()
    assert bet["kind"] == "built" and bet["contracts"] == 1

    settle_paper_bets(client, store)
    text = write_scorecard(store, tmp_path, 1.0, True).read_text()
    assert "Combos the bot built itself" in text and "by number of legs" in text
    assert "| 2 | 1 | 1 | 1 |" in text


def test_cheap_built_combo_becomes_a_pick(tmp_path, monkeypatch):
    monkeypatch.setattr("tradebot.strategy.quotes.time.sleep", lambda s: None)
    store, client = _store(), BuilderClient(quote_yes_bid="0.3000")
    stats = evaluate_combos(_settings(tmp_path, rfq_checks_per_cycle=0), client, store, 50, 0)
    assert stats["combos_built"] == 1 and stats["built_quoted"] == 1 and stats["would_buy"] == 1


def test_builder_errors_do_not_stop_the_cycle(tmp_path):
    class Broken(BuilderClient):
        def get_collection(self, ticker):
            raise RuntimeError("boom")

    stats = evaluate_combos(_settings(tmp_path, rfq_checks_per_cycle=0), Broken(), _store(), 50, 0)
    assert stats["combos_built"] == 0

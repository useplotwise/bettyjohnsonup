from tradebot.collectors.markets import (
    collect_markets,
    collect_orderbooks,
    is_combo_market,
    summarize_orderbook,
)
from tradebot.collectors.sports import parse_scoreboard
from tradebot.kalshi_client import KalshiError
from tradebot.storage import Store


class FakeClient:
    def __init__(self, markets, books):
        self.markets = markets
        self.books = books

    def iter_markets(self, status="open"):
        return iter(self.markets)

    def get_orderbook(self, ticker):
        if ticker not in self.books:
            raise KalshiError("missing")
        return self.books[ticker]


def test_combo_detection():
    assert is_combo_market({"ticker": "KXMVESPORTS-1"})
    assert is_combo_market({"ticker": "X", "mve_collection_ticker": "KXMVE"})
    assert not is_combo_market({"ticker": "KXHIGHNY-25OCT29"})


def test_orderbook_summary_implies_yes_ask():
    snap = summarize_orderbook("T", {"yes": [[40, 10], [42, 5]], "no": [[55, 3]]})
    assert snap["best_yes_bid"] == 42
    assert snap["yes_ask"] == 45
    assert (snap["yes_depth"], snap["no_depth"]) == (15, 3)


def test_empty_orderbook():
    snap = summarize_orderbook("T", {"yes": None, "no": None})
    assert snap["best_yes_bid"] is None and snap["yes_ask"] is None


def test_collect_markets_and_books_store_rows():
    store = Store(":memory:")
    client = FakeClient(
        [{"ticker": "A", "status": "open"}, {"ticker": "KXMVE-B", "status": "open"}],
        {"A": {"yes": [[30, 1]], "no": [[60, 2]]}},
    )
    rows = collect_markets(client, store)
    assert store.count("markets") == 2
    assert sum(r["is_combo"] for r in rows) == 1
    # Missing book for KXMVE-B is skipped, not fatal.
    assert collect_orderbooks(client, store, ["A", "KXMVE-B"]) == 1
    assert store.count("orderbook_snapshots") == 1


def test_parse_espn_scoreboard():
    data = {
        "events": [
            {
                "id": "401",
                "date": "2026-10-29T23:00Z",
                "status": {"type": {"name": "STATUS_FINAL"}},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "24", "team": {"displayName": "Home FC"}},
                            {"homeAway": "away", "score": "17", "team": {"displayName": "Away FC"}},
                        ]
                    }
                ],
            }
        ]
    }
    [game] = parse_scoreboard("football/nfl", data)
    assert game["home_team"] == "Home FC" and game["home_score"] == 24
    assert game["status"] == "STATUS_FINAL"
    store = Store(":memory:")
    assert store.upsert_games([game]) == 1
    assert store.upsert_games([game]) == 1
    assert store.count("sports_games") == 1

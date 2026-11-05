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

    def iter_markets(self, status="open", mve_filter=None):
        if mve_filter == "only":
            return iter([m for m in self.markets if is_combo_market(m)])
        if mve_filter == "exclude":
            return iter([m for m in self.markets if not is_combo_market(m)])
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


def test_collect_markets_respects_limit():
    store = Store(":memory:")
    client = FakeClient([{"ticker": f"M{i}", "status": "open"} for i in range(10)], {})
    assert len(collect_markets(client, store, max_markets=3)) == 3
    assert store.count("markets") == 3


def test_orderbook_summary_dollar_format():
    book = {"yes_dollars": [["0.4000", "10.00"], ["0.4200", "5.00"]], "no_dollars": [["0.5500", "3.00"]]}
    snap = summarize_orderbook("T", book)
    assert (snap["best_yes_bid"], snap["yes_ask"]) == (42, 45)
    assert (snap["yes_depth"], snap["no_depth"]) == (15, 3)


def test_collect_markets_reads_combos_and_regular_separately():
    store = Store(":memory:")
    markets = [{"ticker": f"KXMVE-{i}", "status": "open"} for i in range(5)]
    markets += [{"ticker": f"KXHIGH-{i}", "status": "open"} for i in range(5)]
    rows = collect_markets(FakeClient(markets, {}), store, max_markets=3, max_combos=2)
    assert sum(r["is_combo"] for r in rows) == 2
    assert sum(not r["is_combo"] for r in rows) == 3


class RejectsFilterClient(FakeClient):
    def iter_markets(self, status="open", mve_filter=None):
        if mve_filter:
            def boom():
                raise KalshiError("400 bad param")
                yield
            return boom()
        return iter(self.markets)


def test_collect_markets_falls_back_without_filter():
    store = Store(":memory:")
    client = RejectsFilterClient([{"ticker": "A", "status": "open"}], {})
    assert len(collect_markets(client, store)) == 1


def test_prune_and_compact_keep_history(tmp_path):
    from datetime import datetime, timedelta, timezone

    from tradebot.compact import compact

    path = tmp_path / "bot.sqlite3"
    store = Store(path)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    store.upsert_markets([{"ticker": t, "event_ticker": None, "series_ticker": None, "category": None,
                           "title": None, "status": "open", "is_combo": 0, "close_time": None,
                           "result": None, "raw": {"ticker": t}} for t in ("NEW", "OLD", "BET")])
    store.conn.execute("UPDATE markets SET updated_at = ? WHERE ticker IN ('OLD', 'BET')", (old,))
    store.conn.execute(
        "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee) "
        "VALUES (?, 'pick', 'BET', 'yes', 0.5, 2, 0.02)", (old,))
    for taken in (old, datetime.now(timezone.utc).isoformat(timespec="seconds")):
        store.conn.execute(
            "INSERT INTO orderbook_snapshots (ticker, taken_at, yes_depth, no_depth, raw) "
            "VALUES ('NEW', ?, 0, 0, '{}')", (taken,))
    store.add_decision({"kind": "combo", "ticker": "X", "action": "would_buy", "dry_run": True})
    store.conn.commit()
    store.close()

    compact(path)
    store = Store(path)
    # History kept; markets an open paper bet needs kept; stale markets and books gone.
    assert store.count("paper_bets") == 1 and store.count("decisions") == 1
    assert {r[0] for r in store.conn.execute("SELECT ticker FROM markets")} == {"NEW", "BET"}
    assert store.count("orderbook_snapshots") == 0

    store.conn.execute(
        "INSERT INTO orderbook_snapshots (ticker, taken_at, yes_depth, no_depth, raw) "
        "VALUES ('NEW', ?, 0, 0, '{}')", (old,))
    store.conn.execute("INSERT INTO markets SELECT 'OLD2', event_ticker, series_ticker, category, title, "
                       "status, is_combo, close_time, result, raw, ? FROM markets WHERE ticker='NEW'", (old,))
    store.conn.commit()
    assert store.prune() == {"orderbooks": 1, "markets": 1, "run_log": 0}
    assert store.count("markets") == 2

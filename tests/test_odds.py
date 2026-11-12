import random
from datetime import datetime, timedelta, timezone

from tradebot.config import Settings
from tradebot.odds import BookOdds, consensus, devig, name_matches, refresh_odds
from tradebot.storage import Store
from tradebot.strategy.builder import build_plan, candidate_legs, choose_legs
from tradebot.strategy.runner import bankroll_rules
from tradebot.strategy.value import find_value_bets

SOON = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()


def _event(home, away, books):
    return {
        "id": f"{home}-{away}", "home_team": home, "away_team": away, "commence_time": SOON,
        "bookmakers": [
            {"key": key, "markets": [{"key": "h2h", "outcomes": [{"name": n, "price": p} for n, p in prices.items()]}]}
            for key, prices in books.items()
        ],
    }


LAKERS = _event("Los Angeles Lakers", "Los Angeles Clippers", {
    "pinnacle": {"Los Angeles Lakers": 1.60, "Los Angeles Clippers": 2.50},
    "draftkings": {"Los Angeles Lakers": 1.50, "Los Angeles Clippers": 2.70},
})
ARSENAL = _event("Arsenal", "Chelsea", {"williamhill": {"Arsenal": 2.0, "Chelsea": 4.0, "Draw": 4.0}})


def _market(ticker, title, yes_name, bid, ask, event="EV1"):
    return {"ticker": ticker, "event_ticker": event, "title": title, "yes_sub_title": yes_name,
            "status": "active", "yes_bid": bid, "yes_ask": ask, "close_time": SOON}


def test_devig_and_consensus_prefer_pinnacle():
    probs = devig({"A": 1.60, "B": 2.50})
    assert abs(sum(probs.values()) - 1) < 1e-9 and probs["A"] > probs["B"]
    probs, source = consensus(LAKERS)
    assert source == "pinnacle" and abs(probs["Los Angeles Lakers"] - 0.6098) < 0.001
    probs, source = consensus(ARSENAL)
    assert source == "average of 1 books" and abs(probs["Draw"] - 0.25) < 1e-9


def test_name_matching_is_ordered():
    assert name_matches("Los Angeles L", "Los Angeles Lakers")
    assert not name_matches("Los Angeles L", "Los Angeles Clippers")
    assert name_matches("Chicago", "Chicago Bulls") and not name_matches("", "Chicago Bulls")


class FakeOddsSession:
    def __init__(self, events):
        self.events, self.calls = events, []

    def get(self, url, params, timeout):
        self.calls.append(url)
        events = self.events

        class Resp:
            headers = {"x-requests-remaining": "480"}

            def raise_for_status(self):
                pass

            def json(self):
                return events
        return Resp()


def _book():
    store = Store(":memory:")
    session = FakeOddsSession([LAKERS, ARSENAL])
    stats = refresh_odds(store, "key", ("basketball_nba",), "eu", 720, session)
    assert stats == {"sports_fetched": 1, "games": 2, "lookups_left": "480"}
    # Within the refresh window nothing is fetched again (saves free lookups).
    assert refresh_odds(store, "key", ("basketball_nba",), "eu", 720, session)["sports_fetched"] == 0
    assert len(session.calls) == 1
    assert refresh_odds(store, "", ("soccer_epl",), "eu", 720, session)["sports_fetched"] == 0
    return store, BookOdds.from_store(store)


def test_matching_kalshi_markets_to_games():
    _, book = _book()
    lal = _market("LAL", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 55, 57)
    lac = _market("LAC", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles C", 43, 45)
    tie = _market("TIE", "Arsenal vs Chelsea Winner?", "Tie", 23, 27)
    other = _market("X", "Boston vs Miami Winner?", "Boston", 50, 52)
    assert abs(book.yes_prob(lal) - 0.6098) < 0.001
    assert abs(book.yes_prob(lac) - 0.3902) < 0.001
    assert abs(book.yes_prob(tie) - 0.25) < 1e-9
    assert book.yes_prob(other) is None
    # A "match" far from Kalshi's own price is distrusted (likely a wrong match).
    assert book.yes_prob(_market("LAL2", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 10, 12)) is None
    assert book.rejected == 1
    # leg_prob falls back to Kalshi's price when there's no match.
    assert book.leg_prob(other) == 0.51


def test_value_bets_are_paper_traded(tmp_path):
    store, book = _book()
    store.upsert_markets(
        [{"ticker": m["ticker"], "event_ticker": "EV", "series_ticker": None, "category": None, "title": m["title"],
          "status": "active", "is_combo": 0, "close_time": SOON, "result": None, "raw": m}
         for m in (
             _market("LAL", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 50, 52),  # books 61%
             _market("TIE", "Arsenal vs Chelsea Winner?", "Tie", 25, 27),  # books 25%: no edge either side
         )]
    )
    settings = Settings(log_dir=tmp_path)
    stats = find_value_bets(settings, store, book, bankroll_rules(settings), 0)
    assert stats == {"legs_matched": 2, "value_bets": 1, "bought": 0}
    [bet] = store.conn.execute("SELECT * FROM paper_bets").fetchall()
    assert bet["kind"] == "pregame" and bet["ticker"] == "LAL" and bet["side"] == "yes" and bet["price"] == 0.52
    # Not doubled up while it's open.
    assert find_value_bets(settings, store, book, bankroll_rules(settings), 0)["value_bets"] == 0


def test_builder_prefers_legs_kalshi_sells_cheap():
    _, book = _book()
    markets = [
        _market("LAL", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 50, 52, "G1"),  # value ~9c
        _market("B", "Boston vs Miami Winner?", "Boston", 69, 71, "G2"),
        _market("C", "Denver vs Utah Winner?", "Denver", 74, 76, "G3"),
    ]
    legs = candidate_legs(markets, None, 0.55, 0.95, 0.06, book_prob=book.yes_prob)
    assert legs[0].market_ticker == "LAL" and legs[0].value > 0.05 and abs(legs[0].prob - 0.6098) < 0.001
    for seed in range(10):
        chosen = choose_legs(legs, 2, random.Random(seed))
        assert "LAL" in {l.market_ticker for l in chosen}


def test_build_plan_rotates_safe_then_long_shot():
    settings = Settings(builder_leg_counts=(2, 3), builder_longshot_leg_counts=(4,))
    assert [build_plan(settings, i) for i in range(4)] == [(2, False), (3, False), (4, True), (2, False)]


def test_matches_kalshi_wins_titles_using_the_event():
    """Kalshi's real game markets are titled "Brighton wins"; the opponent comes from the event."""
    store = Store(":memory:")
    brighton = _event("Brighton and Hove Albion", "Brentford", {
        "pinnacle": {"Brighton and Hove Albion": 2.0, "Brentford": 4.0, "Draw": 4.0}})
    refresh_odds(store, "key", ("soccer_epl",), "eu", 720, FakeOddsSession([brighton, ARSENAL]))
    ev = "KXEPLGAME-26NOV08BRIBRE"
    markets = [
        {**_market(f"{ev}-BRI", "Brighton wins", "Brighton", 48, 50, ev), "expected_expiration_time": SOON},
        {**_market(f"{ev}-BRE", "Brentford wins", "Brentford", 24, 26, ev), "expected_expiration_time": SOON},
        {**_market(f"{ev}-TIE", "Tie is the result", "Tie", 24, 26, ev), "expected_expiration_time": SOON},
    ]
    store.upsert_markets(
        [{"ticker": m["ticker"], "event_ticker": ev, "series_ticker": "KXEPLGAME", "category": None,
          "title": m["title"], "status": "active", "is_combo": 0, "close_time": SOON, "result": None, "raw": m}
         for m in markets])
    book = BookOdds.from_store(store)
    assert abs(book.yes_prob(markets[0]) - 0.5) < 1e-9
    assert abs(book.yes_prob(markets[1]) - 0.25) < 1e-9
    assert abs(book.yes_prob(markets[2]) - 0.25) < 1e-9
    # A market whose event has no known opponent isn't guessed at.
    lone = {**_market("X-MIA", "Miami wins", "Miami", 50, 52, "X"), "expected_expiration_time": SOON}
    assert book.yes_prob(lone) is None
    stats = find_value_bets(Settings(), store, book, bankroll_rules(Settings()), 0)
    assert stats["legs_matched"] == 3


def test_no_value_bets_once_the_game_has_started():
    """In-game Kalshi prices move with the score; pregame odds must not be compared to them."""
    store, _ = _book()
    lal = _market("LAL", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 50, 52)
    started = BookOdds.from_store(store)
    started.now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=1)
    assert started.yes_prob(lal) is None
    almost = BookOdds.from_store(store)
    almost.now = datetime.now(timezone.utc) + timedelta(hours=4, minutes=58)
    assert almost.yes_prob(lal) is None  # within 5 minutes of the start
    assert BookOdds.from_store(store).yes_prob(lal) is not None


def test_value_bet_is_never_repeated_or_made_on_stale_markets(tmp_path):
    store, book = _book()
    lal = _market("LAL", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 50, 52)
    store.upsert_markets([{"ticker": "LAL", "event_ticker": "EV", "series_ticker": None, "category": None,
                           "title": lal["title"], "status": "active", "is_combo": 0, "close_time": SOON,
                           "result": None, "raw": lal}])
    settings = Settings(log_dir=tmp_path)
    assert find_value_bets(settings, store, book, bankroll_rules(settings), 0)["value_bets"] == 1
    # After it settles, the same market isn't bet again.
    store.conn.execute("UPDATE paper_bets SET status = 'lost'")
    store.conn.commit()
    assert find_value_bets(settings, store, BookOdds.from_store(store), bankroll_rules(settings), 0)["value_bets"] == 0
    # A saved market the recent scans didn't refresh is ignored.
    store.conn.execute("DELETE FROM paper_bets")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE markets SET updated_at = ?", (old,))
    store.conn.commit()
    assert find_value_bets(settings, store, BookOdds.from_store(store), bankroll_rules(settings), 0)["legs_matched"] == 0


def test_one_value_bet_per_game_not_per_team(tmp_path):
    """Live data showed the bot betting both teams of one game (same outcome twice)."""
    store, book = _book()
    lal = _market("LAL", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles L", 50, 52, "EVGAME")
    lac = _market("LAC", "Los Angeles L vs Los Angeles C Winner?", "Los Angeles C", 40, 42, "EVGAME")
    store.upsert_markets([{"ticker": m["ticker"], "event_ticker": "EVGAME", "series_ticker": None,
                           "category": None, "title": m["title"], "status": "active", "is_combo": 0,
                           "close_time": SOON, "result": None, "raw": m} for m in (lal, lac)])
    settings = Settings(log_dir=tmp_path)
    stats = find_value_bets(settings, store, book, bankroll_rules(settings), 0)
    bets = store.conn.execute("SELECT ticker FROM paper_bets").fetchall()
    assert len(bets) <= 1, [b[0] for b in bets]
    assert stats["value_bets"] == len(bets)

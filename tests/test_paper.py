from tradebot.kalshi_client import KalshiError
from tradebot.paper import open_paper_bet, paper_summary, settle_paper_bets
from tradebot.scorecard import write_scorecard
from tradebot.storage import Store


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def get_market(self, ticker):
        market = self.markets.get(ticker)
        if market is None:
            raise KalshiError(f"GET /markets/{ticker} -> 404: not found")
        return market


def test_open_is_deduplicated_and_validated():
    store = Store(":memory:")
    assert open_paper_bet(store, "pick", "T", "yes", 0.25, 20, win_prob=0.3)
    assert not open_paper_bet(store, "pick", "T", "yes", 0.25, 20)
    assert open_paper_bet(store, "watch", "T", "yes", 0.25, 1)
    assert not open_paper_bet(store, "pick", "U", "yes", 0.0, 5)
    assert not open_paper_bet(store, "pick", "U", "yes", 0.5, 0)


def test_settles_wins_losses_and_voids():
    store = Store(":memory:")
    open_paper_bet(store, "pick", "WIN", "yes", 0.25, 20, win_prob=0.4)  # fee 0.27
    open_paper_bet(store, "pick", "LOSE", "yes", 0.50, 4, win_prob=0.6)  # fee 0.07
    open_paper_bet(store, "pick", "VOID", "yes", 0.50, 4)
    open_paper_bet(store, "pick", "OPEN", "yes", 0.50, 4)
    open_paper_bet(store, "pick", "GONE", "yes", 0.50, 4)
    client = FakeClient({
        "WIN": {"result": "yes", "status": "settled"},
        "LOSE": {"result": "no", "status": "settled"},
        "VOID": {"result": "", "status": "finalized"},
        "OPEN": {"result": "", "status": "active"},
    })
    stats = settle_paper_bets(client, store)
    assert stats == {"open": 5, "checked": 5, "settled": 3}

    pnl = dict(store.conn.execute("SELECT ticker, pnl FROM paper_bets WHERE status != 'open'").fetchall())
    assert pnl == {"WIN": 20 - 5 - 0.27, "LOSE": -2.07, "VOID": 0.0}

    s = paper_summary(store, "pick")
    assert s["open"] == 2 and s["settled"] == 2 and s["won"] == 1
    assert s["pnl"] == round(14.73 - 2.07, 2)
    assert s["expected_win_rate"] == 0.5 and s["actual_win_rate"] == 0.5

    # Settled bets are not checked again.
    assert settle_paper_bets(client, store)["checked"] == 2


def test_check_limit_rotates_through_open_bets():
    store = Store(":memory:")
    for t in ("A", "B", "C"):
        open_paper_bet(store, "watch", t, "yes", 0.5, 1)
    client = FakeClient({t: {"status": "active"} for t in "ABC"})
    settle_paper_bets(client, store, max_checks=2)
    unchecked = store.conn.execute("SELECT ticker FROM paper_bets WHERE checked_at IS NULL").fetchall()
    assert [r[0] for r in unchecked] == ["C"]
    client.markets["C"] = {"result": "no", "status": "settled"}
    assert settle_paper_bets(client, store, max_checks=1)["settled"] == 1


def test_scorecard_shows_paper_results(tmp_path):
    store = Store(":memory:")
    open_paper_bet(store, "pick", "WIN", "yes", 0.25, 20, win_prob=0.4)
    settle_paper_bets(FakeClient({"WIN": {"result": "yes", "status": "settled"}}), store)
    text = write_scorecard(store, tmp_path, 1.16, True).read_text()
    assert "Paper trading" in text
    assert "Pretend profit/loss: $+14.73" in text
    assert "expected to win 40%" in text and "actually won 100%" in text

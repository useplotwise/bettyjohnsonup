"""The one-time repair of records made while combo quotes were read backwards."""

import json

import pytest

from tradebot.live import bot_ledger
from tradebot.paper import paper_summary, settle_paper_bets
from tradebot.pricing import kalshi_fee
from tradebot.scorecard import write_scorecard
from tradebot.storage import Store, fix_backwards_combo_bets


def _bet(store, kind, ticker, price, contracts, status="open", pnl=None, side="yes", win_prob=0.45, raw=None):
    store.conn.execute(
        "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee, win_prob, edge, status, "
        "pnl, raw) VALUES ('2026-11-13T00:00:00+00:00', ?, ?, ?, ?, ?, ?, ?, 0.2, ?, ?, ?)",
        (kind, ticker, side, price, contracts, kalshi_fee(price, contracts), win_prob, status, pnl,
         json.dumps(raw or {})),
    )


def _quote_seen(store, ticker, yes_bid, no_bid, action="price_check"):
    store.add_decision({"kind": "combo", "ticker": ticker, "action": action, "dry_run": True,
                        "raw": {"quote": {"yes_bid_dollars": yes_bid, "no_bid_dollars": no_bid}}})


def _old_database(path):
    """A database as the old code left it: bets recorded as YES at the maker's YES bid."""
    store = Store(path)
    live = {"legs": [], "quote_id": "q", "quoted_cost": 0.35}
    # Real bets. Kalshi's fills: NO at 0.65 each, about $1 a bet.
    _bet(store, "live", "KXMVE-HIT", 0.35, 1.47, status="won", pnl=0.95, raw=live)    # resolved YES: NO lost
    _bet(store, "live", "KXMVE-MISS", 0.35, 1.47, status="lost", pnl=-0.54, raw=live)  # resolved NO: NO won
    _bet(store, "live", "KXMVE-OPEN", 0.35, 1.47, raw=live)
    _bet(store, "live", "SINGLE-GAME", 0.50, 2, raw={"order_id": "o1", "single": True})
    # Pretend bets.
    _bet(store, "pick", "KXMVE-FAKE-EDGE", 0.30, 3, status="won", pnl=2.05)
    _quote_seen(store, "KXMVE-FAKE-EDGE", "0.3000", "0.0000")       # maker only bid for YES
    _bet(store, "watch", "KXMVE-REAL-PRICE", 0.825, 1, status="lost", pnl=-0.84)
    _quote_seen(store, "KXMVE-REAL-PRICE", "0.3020", "0.1750")      # YES really cost 1 - 0.175
    _bet(store, "watch", "KXMVE-NO-QUOTE-KEPT", 0.40, 1)
    _bet(store, "built", "KXMVE-BUILT", 0.25, 1, status="lost", pnl=-0.27)
    _quote_seen(store, "KXMVE-BUILT", "0.2500", "0", action="built_combo")
    _bet(store, "weather", "KXHIGHCHI", 0.20, 5, status="lost", pnl=-1.06)
    store.conn.execute("PRAGMA user_version = 0")
    store.conn.commit()
    store.close()


def test_real_combo_bets_are_rewritten_to_what_kalshi_filled(tmp_path):
    path = tmp_path / "bot.sqlite3"
    _old_database(path)
    store = Store(path)
    rows = {r["ticker"]: r for r in store.conn.execute("SELECT * FROM paper_bets WHERE kind = 'live'")}
    cost = round(0.65 * 1.47 + kalshi_fee(0.65, 1.47), 2)

    hit, miss, still_open = rows["KXMVE-HIT"], rows["KXMVE-MISS"], rows["KXMVE-OPEN"]
    assert (hit["side"], hit["price"], hit["status"]) == ("no", 0.65, "lost") and hit["pnl"] == -cost
    assert (miss["side"], miss["price"], miss["status"]) == ("no", 0.65, "won")
    assert miss["pnl"] == pytest.approx(1.47 - cost)
    assert (still_open["side"], still_open["status"], still_open["pnl"]) == ("no", "open", None)
    assert hit["win_prob"] == pytest.approx(0.55) and "corrected" in json.loads(hit["raw"])
    # A real single-market bet was never affected.
    assert (rows["SINGLE-GAME"]["side"], rows["SINGLE-GAME"]["price"]) == ("yes", 0.50)

    ledger = bot_ledger(store)
    assert ledger["profit"] == pytest.approx(round(1.47 - 2 * cost, 2)) and ledger["won"] == 1

    # The bet still open settles as the NO bet it really is.
    class Kalshi:
        def get_market(self, ticker):
            return {"ticker": ticker, "status": "settled", "result": "no"}

    settle_paper_bets(Kalshi(), store)
    [row] = store.conn.execute("SELECT * FROM paper_bets WHERE ticker = 'KXMVE-OPEN'").fetchall()
    assert row["status"] == "won" and row["pnl"] == pytest.approx(1.47 - cost)


def test_pretend_combo_bets_with_backwards_prices_stop_counting(tmp_path):
    path = tmp_path / "bot.sqlite3"
    _old_database(path)
    store = Store(path)
    kinds = {r["ticker"]: (r["kind"], r["status"]) for r in store.conn.execute("SELECT * FROM paper_bets")}
    assert kinds["KXMVE-FAKE-EDGE"] == ("pick_old", "won")
    assert kinds["KXMVE-BUILT"] == ("built_old", "lost")
    assert kinds["KXMVE-NO-QUOTE-KEPT"] == ("watch_old", "retired")  # no proof its price was real
    assert kinds["KXMVE-REAL-PRICE"] == ("watch", "lost")            # priced at 1 - no_bid: still counts
    assert kinds["KXHIGHCHI"] == ("weather", "lost")

    assert paper_summary(store, "pick")["settled"] == 0 and paper_summary(store, "watch")["settled"] == 1
    text = write_scorecard(store, tmp_path, 1.76, True).read_text()
    assert "Old combo bets set aside: 3" in text and "$+2.05" not in text


def test_the_repair_runs_once(tmp_path):
    path = tmp_path / "bot.sqlite3"
    _old_database(path)
    Store(path).close()
    store = Store(path)
    before = [tuple(r) for r in store.conn.execute("SELECT * FROM paper_bets ORDER BY id")]
    assert fix_backwards_combo_bets(store.conn)["real_bets_corrected"] == 0
    store.close()
    store = Store(path)
    assert [tuple(r) for r in store.conn.execute("SELECT * FROM paper_bets ORDER BY id")] == before
    # Bets made after the fix are left alone.
    _bet(store, "pick", "KXMVE-NEW", 0.30, 3)
    store.conn.commit()
    store.close()
    assert Store(path).conn.execute("SELECT kind FROM paper_bets WHERE ticker = 'KXMVE-NEW'").fetchone()[0] == "pick"


def test_shrinking_the_database_does_not_skip_or_repeat_the_repair(tmp_path):
    from tradebot.compact import compact

    path = tmp_path / "bot.sqlite3"
    _old_database(path)        # never opened by the fixed code yet
    compact(path)
    store = Store(path)        # so the repair still has to run here
    assert store.conn.execute("SELECT side FROM paper_bets WHERE ticker = 'KXMVE-HIT'").fetchone()[0] == "no"
    # A real bet placed by the fixed code is a YES bet and must stay one.
    _bet(store, "live", "KXMVE-AFTER", 0.30, 3, raw={"legs": [], "quote_id": "q9", "side_from_fills": True})
    store.conn.commit()
    store.close()
    compact(path)
    row = Store(path).conn.execute("SELECT side, price FROM paper_bets WHERE ticker = 'KXMVE-AFTER'").fetchone()
    assert tuple(row) == ("yes", 0.30)

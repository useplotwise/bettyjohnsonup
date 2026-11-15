import json
import random

from tradebot.edge_report import MIN_TRUSTED, Bet, ReadOnly, build_report, other_side_bets, summarize
from tradebot.pricing import kalshi_fee
from tradebot.storage import Store


def _bet(store, kind, ticker, price, won, legs=None, side="yes", edge=0.05, contracts=1):
    store.conn.execute(
        "INSERT INTO paper_bets (opened_at, kind, ticker, side, price, contracts, fee, win_prob, edge, status, "
        "pnl, raw) VALUES ('2026-11-13T00:00:00+00:00', ?, ?, ?, ?, ?, ?, 0.5, ?, ?, 0, ?)",
        (kind, ticker, side, price, contracts, kalshi_fee(price, contracts), edge, "won" if won else "lost",
         json.dumps({"legs": legs or []})),
    )


def test_a_group_only_wins_if_it_beats_the_luck_range():
    fair_coin_at_60c = [Bet(0.60, 1, 0.02, won=i % 2 == 0) for i in range(400)]
    s = summarize(fair_coin_at_60c)
    assert s["n"] == 400 and s["won"] == 0.5 and round(s["need"], 2) == 0.62
    assert s["per_dollar"] < 0 and s["verdict"] == ""

    sure_thing = [Bet(0.60, 1, 0.02, won=i % 10 != 0) for i in range(400)]  # wins 90% at a 62% break-even
    assert summarize(sure_thing)["verdict"] == "WINS"
    # The same record on too few bets is never called a win.
    assert summarize(sure_thing[:MIN_TRUSTED - 1])["verdict"] == ""
    # Ahead, but by less than the luck range: 6 wins in 200 long shots that cost 2 cents with the fee.
    lucky = [Bet(0.01, 1, 0.01, won=i < 6) for i in range(200)]
    assert summarize(lucky)["per_dollar"] > 0 and summarize(lucky)["verdict"] == "maybe (could be luck)"


def test_report_groups_combos_and_finds_the_planted_edge(tmp_path):
    rng = random.Random(7)
    store = Store(tmp_path / "bot.sqlite3")
    nba = [("KXNBAGAME-26NOV12LALLAC-LAL", "yes"), ("KXNBAGAME-26NOV12BOSNYK-BOS", "yes")]
    same_game = [("KXNFLGAME-26NOV15KCDEN-KC", "yes"), ("KXNFLGAME-26NOV15KCDEN-DEN", "no"),
                 ("KXNFLTOTAL-26NOV15KCDEN-T45", "yes")]
    for i in range(300):   # overpriced: wins 50% at 60 cents
        _bet(store, "watch", f"KXMVE-A{i}", 0.60, rng.random() < 0.5, nba)
    for i in range(300):   # the planted edge: wins 95% at 80 cents
        _bet(store, "built", f"KXMVE-B{i}", 0.80, rng.random() < 0.95, same_game)
    for i in range(30):
        _bet(store, "weather", f"KXHIGHCHI-{i}", 0.20, i < 3, side="no")
    _bet(store, "pick", "KXMVE-A1", 0.60, True, nba, contracts=5)   # same combo as a watch bet: not counted twice
    # A maker bid 30c for YES on the first 100 overpriced combos: the other side is NO at 70c.
    for i in range(100):
        store.add_decision({"kind": "combo", "ticker": f"KXMVE-A{i}", "action": "price_check", "dry_run": True,
                            "raw": {"legs": nba, "quote": {"yes_bid_dollars": "0.3000", "no_bid_dollars": "0.4000"}}})
    store.conn.commit()

    assert len(other_side_bets(store)) == 100 and all(b.price == 0.70 for b in other_side_bets(store))
    store.close()

    text = "\n".join(build_report(ReadOnly(tmp_path / "bot.sqlite3")))
    yes_side, no_side = text.split("== Combos: the other side")[0], text.split("== Combos: the other side")[1]

    def row(section, label):
        [line] = [ln for ln in section.splitlines() if ln.strip().startswith(label)]
        return line.split()

    assert row(yes_side, "all of them")[3] == "600"
    assert row(yes_side, "50-75c")[1] == "300" and "WINS" not in row(yes_side, "50-75c")
    assert row(yes_side, "75-90c")[-1] == "WINS"
    assert row(yes_side, "3 legs")[-1] == "WINS" and row(yes_side, "legs share a game")[-1] == "WINS"
    assert row(yes_side, "all different games")[3] == "300"
    assert row(no_side, "all of them")[3] == "100" and row(no_side, "50-75c")[1] == "100"
    assert row(yes_side, "NBA")[1] == "300" and row(yes_side, "NFL")[-1] == "WINS"
    assert "Weather forecast bets" in text and "too few finished to say" in text
    assert "Groups that win even after allowing for luck:" in text
    assert max(len(ln) for ln in text.splitlines()) <= 92   # fits the server console without wrapping

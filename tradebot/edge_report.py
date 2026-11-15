"""Where, if anywhere, do the bot's pretend bets really win?

    sudo -u tradebot python3 -m tradebot.edge_report

Read-only. Sorts every finished pretend bet into groups (by price, number of
legs, sport, and so on) and shows, for each group, what betting $1 on every
bet in it would have made. A group only counts as a real edge if it still
wins after allowing for luck, so each line shows a plus-or-minus range.

Columns:
  bets    finished bets in the group
  won     how often they won
  need    how often they had to win to break even (price plus Kalshi's fee)
  per $1  average profit for each $1 bet (negative = loss)
  +/-     the luck range: the true figure is very likely within this much
  A group is marked WINS only if per $1 minus the range is still above zero.
"""

import json
import math
import sqlite3
from collections import defaultdict
from typing import Callable, Iterable

from .config import load_settings
from .pricing import kalshi_fee
from .storage import Store

MIN_SHOWN = 20     # groups smaller than this are too small to say anything
MIN_TRUSTED = 100  # groups smaller than this are never marked as winning

PRICE_BANDS = ((0.10, "under 10c"), (0.25, "10-25c"), (0.50, "25-50c"), (0.75, "50-75c"),
               (0.90, "75-90c"), (1.01, "90c and up"))
EDGE_BANDS = ((0.0, "bot saw no edge"), (0.10, "edge 0-10%"), (0.25, "edge 10-25%"), (9e9, "edge 25%+"))


class Bet:
    """One finished bet, per $1 staked."""

    __slots__ = ("price", "won", "ret", "need", "win_prob", "edge", "raw", "side", "kind")

    def __init__(self, price, contracts, fee, won, win_prob=None, edge=None, raw=None, side="yes", kind=""):
        stake = price * contracts + fee
        self.price, self.won = price, won
        self.ret = ((contracts if won else 0) - stake) / stake
        self.need = stake / contracts
        self.win_prob, self.edge, self.raw, self.side, self.kind = win_prob, edge, raw or {}, side, kind


def _band(value: float, bands) -> str:
    for limit, label in bands:
        if value < limit:
            return label
    return bands[-1][1]


def summarize(bets: list[Bet]) -> dict:
    n = len(bets)
    mean = sum(b.ret for b in bets) / n
    spread = math.sqrt(sum((b.ret - mean) ** 2 for b in bets) / (n - 1)) if n > 1 else 0.0
    luck = 2 * spread / math.sqrt(n)
    verdict = ""
    if n >= MIN_TRUSTED and mean - luck > 0:
        verdict = "WINS"
    elif n >= MIN_TRUSTED and mean > 0:
        verdict = "maybe (could be luck)"
    return {"n": n, "won": sum(b.won for b in bets) / n, "need": sum(b.need for b in bets) / n,
            "per_dollar": mean, "luck": luck, "verdict": verdict}


def _line(label: str, bets: list[Bet]) -> str:
    s = summarize(bets)
    return (f"  {label:<24}{s['n']:>6}{s['won']:>7.0%}{s['need']:>7.0%}"
            f"{s['per_dollar']:>+9.2f}{s['luck']:>7.2f}  {s['verdict']}").rstrip()


def table(title: str, bets: Iterable[Bet], key: Callable[[Bet], str | None], order: list[str] | None = None,
          top: int | None = None) -> list[str]:
    groups: dict[str, list[Bet]] = defaultdict(list)
    for b in bets:
        label = key(b)
        if label is not None:
            groups[label].append(b)
    shown = {k: v for k, v in groups.items() if len(v) >= MIN_SHOWN}
    if not shown:
        return []
    if order:
        labels = [k for k in order if k in shown]
    else:
        labels = sorted(shown, key=lambda k: -len(shown[k]))[:top]
    return [f"{title}:"] + [_line(k, shown[k]) for k in labels]


# -- how a bet is grouped ----------------------------------------------------

def _legs(bet: Bet) -> list:
    return bet.raw.get("legs") or []


def _leg_count(bet: Bet) -> str | None:
    n = len(_legs(bet))
    return None if n < 2 else (f"{n} legs" if n < 5 else "5+ legs")


def _game(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def _game_mix(bet: Bet) -> str | None:
    games = [_game(str(leg[0])) for leg in _legs(bet)]
    if len(games) < 2:
        return None
    return "legs share a game" if len(set(games)) < len(games) else "all different games"


LEAGUES = ("WNBA", "NBA", "NCAAF", "NCAAB", "NCAAM", "NFL", "NHL", "MLB", "MLS", "EPL", "UFC", "ATP", "WTA",
           "LALIGA", "UCL", "F1", "PGA")


def _league(ticker: str) -> str:
    series = ticker.split("-", 1)[0].removeprefix("KX")
    return next((name for name in LEAGUES if name in series), series[:14])


def _sport(bet: Bet) -> str | None:
    leagues = {_league(str(leg[0])) for leg in _legs(bet)}
    if not leagues:
        return None
    return leagues.pop() if len(leagues) == 1 else "mixed sports"


def _edge(bet: Bet) -> str | None:
    return None if bet.edge is None else _band(bet.edge, EDGE_BANDS)


# -- loading -----------------------------------------------------------------

def load_bets(store: Store, kinds: tuple[str, ...]) -> list[Bet]:
    marks = ", ".join("?" * len(kinds))
    rows = store.conn.execute(
        f"SELECT * FROM paper_bets WHERE status IN ('won', 'lost') AND kind IN ({marks})", kinds
    ).fetchall()
    return [Bet(r["price"], r["contracts"], r["fee"], r["status"] == "won", r["win_prob"], r["edge"],
                json.loads(r["raw"]) if r["raw"] else {}, r["side"], r["kind"])
            for r in rows if r["contracts"] and 0 < r["price"] < 1]


def other_side_bets(store: Store) -> list[Bet]:
    """One pretend NO contract for every combo a maker bid to buy YES on: NO costs 1 - that bid."""
    result: dict[str, str] = {}
    for r in store.conn.execute(
        "SELECT ticker, side, status FROM paper_bets WHERE status IN ('won', 'lost') AND ticker LIKE 'KXMVE%'"
    ):
        other = "no" if r["side"] == "yes" else "yes"
        result[r["ticker"]] = r["side"] if r["status"] == "won" else other
    bets, seen = [], set()
    for r in store.conn.execute(
        "SELECT ticker, raw FROM decisions WHERE action IN ('price_check', 'built_combo') "
        "AND raw LIKE '%yes_bid%' ORDER BY id DESC"
    ):
        if r["ticker"] in seen or r["ticker"] not in result:
            continue
        raw = json.loads(r["raw"]) or {}
        quote = raw.get("quote") or {}
        try:
            yes_bid = float(quote.get("yes_bid_dollars") or quote.get("yes_bid") or 0)
        except (TypeError, ValueError):
            continue
        if not 0 < yes_bid < 1:
            continue
        seen.add(r["ticker"])
        price = round(1 - yes_bid, 4)
        bets.append(Bet(price, 1, kalshi_fee(price), result[r["ticker"]] == "no", raw={"legs": raw.get("legs")},
                        side="no"))
    return bets


# -- the report --------------------------------------------------------------

HEADER = f"  {'':<24}{'bets':>6}{'won':>7}{'need':>7}{'per $1':>9}{'+/-':>7}"
OTHER_KINDS = (("pregame", "Single game bets"), ("value", "Old single bets"),
               ("weather", "Weather forecast bets"), ("weather_today", "Same-day thermometer"))


def build_report(store: Store) -> list[str]:
    price_order = [label for _, label in PRICE_BANDS]
    lines = ["EDGE REPORT: what $1 on every bet in a group would have made", HEADER, ""]

    combos = load_bets(store, ("watch", "built"))
    lines.append("== Combos: buying YES at the real price ==")
    if combos:
        lines.append(_line("all of them", combos))
        lines += table("by price", combos, lambda b: _band(b.price, PRICE_BANDS), price_order)
        lines += table("by number of legs", combos, _leg_count, ["2 legs", "3 legs", "4 legs", "5+ legs"])
        lines += table("by game mix", combos, _game_mix)
        lines += table("by sport", combos, _sport, top=6)
        lines += table("by the bot's own edge", combos, _edge, [label for _, label in EDGE_BANDS])
    else:
        lines.append("  no finished bets yet")

    other = other_side_bets(store)
    lines += ["", "== Combos: the other side (NO, at 1 minus the maker's YES bid) =="]
    if other:
        lines.append(_line("all of them", other))
        lines += table("by NO price", other, lambda b: _band(b.price, PRICE_BANDS), price_order)
        lines += table("by number of legs", other, _leg_count, ["2 legs", "3 legs", "4 legs", "5+ legs"])
    else:
        lines.append("  no finished bets yet")

    lines += ["", "== Other kinds of bet =="]
    for kind, name in OTHER_KINDS:
        bets = load_bets(store, (kind,))
        if len(bets) < MIN_SHOWN:
            lines.append(f"  {name:<24}{len(bets):>6}   too few finished to say")
            continue
        lines.append(_line(name, bets))
        bands: dict[str, list[Bet]] = defaultdict(list)
        for b in bets:
            bands[_band(b.price, PRICE_BANDS)].append(b)
        if sum(len(v) >= MIN_SHOWN for v in bands.values()) > 1:
            lines += [_line(f"  at {label}", bands[label]) for label in price_order
                      if len(bands[label]) >= MIN_SHOWN]

    winners = [ln for ln in lines if ln.endswith("WINS")]
    lines += ["", f"Groups that win even after allowing for luck: {len(winners)}"]
    return lines


class ReadOnly:
    """The bot's database opened read-only, so the report can never block or change the running bot."""

    def __init__(self, path):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        self.conn.row_factory = sqlite3.Row


def main() -> None:
    store = ReadOnly(load_settings().db_path)
    print("\n".join(build_report(store)))
    store.conn.close()


if __name__ == "__main__":
    main()

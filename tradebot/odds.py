"""Sportsbook odds: a second opinion on game results, from The Odds API.

The bot's own estimate of a leg is Kalshi's price for it. Sharp sportsbooks
(Pinnacle above all) are very accurate, so when Kalshi sells a result cheaper
than the books say it should cost, that's a real edge.

- Odds are fetched per sport every ODDS_REFRESH_MINUTES (to stay inside the
  free plan's 500 lookups a month) and stored in the database.
- Each bookmaker's prices are turned into probabilities with its margin
  removed ("de-vigged"). Pinnacle is used when it has the game; otherwise the
  average of the books that do.
- Kalshi markets are matched to games by team names and date. A match is only
  trusted when it's unambiguous and within MAX_GAP of Kalshi's own price, so a
  wrong match can't create a fake edge. Unmatched legs keep Kalshi's price.

Only game winners (moneyline, including soccer draws) are matched for now.
"""

import json
import logging
import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone

import requests

from .pricing import implied_yes_prob, settled_result
from .storage import Store, utcnow

log = logging.getLogger(__name__)

ODDS_BASE = "https://api.the-odds-api.com/v4"
SHARP_BOOK = "pinnacle"
MAX_GAP = 0.25
MAX_DAYS_APART = 2
TIE_WORDS = {"tie", "draw"}
STOP_WORDS = {"winner", "win", "wins", "will", "the", "fc", "sc", "cf"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sportsbook_odds (
    sport TEXT NOT NULL,
    event_id TEXT NOT NULL,
    commence_time TEXT,
    home_team TEXT,
    away_team TEXT,
    probs TEXT NOT NULL,
    source TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (sport, event_id)
);
CREATE TABLE IF NOT EXISTS odds_fetches (
    sport TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    remaining TEXT
);
"""


# -- turning bookmaker prices into probabilities -----------------------------

def devig(prices: dict[str, float]) -> dict[str, float]:
    """Decimal odds -> probabilities that sum to 1 (bookmaker margin removed)."""
    raw = {name: 1 / p for name, p in prices.items() if p and p > 1}
    total = sum(raw.values())
    return {name: r / total for name, r in raw.items()} if total else {}


def consensus(event: dict) -> tuple[dict[str, float], str]:
    """Probability of each outcome: Pinnacle if it has the game, else the average book."""
    books = {}
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") == "h2h":
                probs = devig({o["name"]: o.get("price") for o in market.get("outcomes", [])})
                if probs:
                    books[book.get("key", "?")] = probs
    if not books:
        return {}, ""
    if SHARP_BOOK in books:
        return books[SHARP_BOOK], SHARP_BOOK
    names = set().union(*books.values())
    avg = {n: sum(b.get(n, 0) for b in books.values()) / len(books) for n in names}
    total = sum(avg.values())
    return {n: p / total for n, p in avg.items()}, f"average of {len(books)} books"


# -- fetching and storing ----------------------------------------------------

def fetch_odds(api_key: str, sport: str, regions: str, session=None) -> tuple[list[dict], str | None]:
    http = session or requests
    resp = http.get(
        f"{ODDS_BASE}/sports/{sport}/odds",
        params={"apiKey": api_key, "regions": regions, "markets": "h2h", "oddsFormat": "decimal"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json(), resp.headers.get("x-requests-remaining")


def refresh_odds(store: Store, api_key: str, sports: tuple[str, ...], regions: str,
                 refresh_minutes: float, session=None) -> dict:
    """Fetch odds for each sport whose last fetch is older than refresh_minutes."""
    store.conn.executescript(SCHEMA)
    stats = {"sports_fetched": 0, "games": 0, "lookups_left": None}
    if not api_key:
        return stats
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=refresh_minutes)).isoformat(timespec="seconds")
    for sport in sports:
        row = store.conn.execute("SELECT fetched_at FROM odds_fetches WHERE sport = ?", (sport,)).fetchone()
        if row and row[0] >= cutoff:
            continue
        try:
            events, remaining = fetch_odds(api_key, sport, regions, session)
        except requests.RequestException as exc:
            log.warning("odds %s: %s", sport, exc)
            continue
        now = utcnow()
        with store.conn:
            for ev in events:
                probs, source = consensus(ev)
                if not probs:
                    continue
                store.conn.execute(
                    """INSERT INTO sportsbook_odds (sport, event_id, commence_time, home_team, away_team,
                           probs, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(sport, event_id) DO UPDATE SET probs = excluded.probs,
                           source = excluded.source, commence_time = excluded.commence_time,
                           fetched_at = excluded.fetched_at""",
                    (sport, ev["id"], ev.get("commence_time"), ev.get("home_team"), ev.get("away_team"),
                     json.dumps(probs), source, now),
                )
                stats["games"] += 1
            store.conn.execute(
                "INSERT INTO odds_fetches (sport, fetched_at, remaining) VALUES (?, ?, ?) "
                "ON CONFLICT(sport) DO UPDATE SET fetched_at = excluded.fetched_at, remaining = excluded.remaining",
                (sport, now, remaining),
            )
        stats["sports_fetched"] += 1
        stats["lookups_left"] = remaining
        log.info("odds %s: %d games (lookups left this month: %s)", sport, len(events), remaining)
    return stats


# -- matching Kalshi markets to games ----------------------------------------

@lru_cache(maxsize=20000)
def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in STOP_WORDS)


def name_matches(short: str, full: str) -> bool:
    """Kalshi's short name ("Los Angeles L") fits a full name ("Los Angeles Lakers").

    Each short word must start a later word of the full name, in order, so
    "Los Angeles L" fits "Los Angeles Lakers" but not "Los Angeles Clippers".
    """
    short_t, full_t = _tokens(short), _tokens(full)
    if not short_t:
        return False
    i = 0
    for s in short_t:
        while i < len(full_t) and not full_t[i].startswith(s):
            i += 1
        if i == len(full_t):
            return False
        i += 1
    return True


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class BookOdds:
    """Answers "what do the sportsbooks say about this Kalshi market?"."""

    def __init__(self, games: list[dict]):
        self.games = games
        self.matched = 0
        self.rejected = 0
        self._cache: dict[str, float | None] = {}

    @classmethod
    def from_store(cls, store: Store, max_age_hours: float = 36) -> "BookOdds":
        store.conn.executescript(SCHEMA)
        since = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        rows = store.conn.execute(
            "SELECT * FROM sportsbook_odds WHERE fetched_at >= ?", (since,)
        ).fetchall()
        games = [{"home": r["home_team"], "away": r["away_team"], "commence": _parse_time(r["commence_time"]),
                  "probs": json.loads(r["probs"]), "source": r["source"]} for r in rows]
        return cls(games)

    def yes_prob(self, market: dict) -> float | None:
        """Sportsbook probability that this market resolves YES, or None if no safe match."""
        key = market.get("ticker")
        if key and key in self._cache:
            return self._cache[key]
        result = self._match(market)
        if key:
            self._cache[key] = result
        return result

    def _match(self, market: dict) -> float | None:
        yes_name = market.get("yes_sub_title") or market.get("subtitle") or ""
        title = market.get("title") or ""
        if not yes_name or not self.games or not _VS.search(title):
            return None
        when = _parse_time(market.get("expected_expiration_time") or market.get("close_time"))
        found = []
        for game in self.games:
            if when and game["commence"] and abs(when - game["commence"]) > timedelta(days=MAX_DAYS_APART):
                continue
            teams = [game["home"], game["away"]]
            if not _same_game(title, *teams):
                continue
            if set(_tokens(yes_name)) & TIE_WORDS:
                draw = next((p for n, p in game["probs"].items() if n.lower() in TIE_WORDS), None)
                if draw is not None:
                    found.append(draw)
                continue
            hits = [t for t in teams if name_matches(yes_name, t)]
            if len(hits) == 1 and hits[0] in game["probs"]:
                found.append(game["probs"][hits[0]])
        if len(found) != 1:
            return None
        kalshi = implied_yes_prob(market)
        if kalshi is not None and abs(found[0] - kalshi) > MAX_GAP:
            self.rejected += 1
            return None
        self.matched += 1
        return found[0]

    def leg_prob(self, market: dict) -> float | None:
        """Best estimate for a leg: settled result, else sportsbooks, else Kalshi's price."""
        if settled_result(market):
            return implied_yes_prob(market)
        book = self.yes_prob(market)
        return book if book is not None else implied_yes_prob(market)


_VS = re.compile(r"\s(?:vs\.?|v\.?|at|@)\s", re.IGNORECASE)


def _same_game(title: str, home: str, away: str) -> bool:
    """Does the Kalshi title ("Los Angeles L vs Los Angeles C Winner?") name exactly these two teams?"""
    sides = re.split(r"\s+(?:vs\.?|v\.?|at|@)\s+", title, flags=re.IGNORECASE)
    if len(sides) != 2:
        return False
    a, b = sides
    straight = name_matches(a, home) and name_matches(b, away)
    crossed = name_matches(a, away) and name_matches(b, home)
    return straight != crossed  # exactly one way round, so it's unambiguous

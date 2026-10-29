"""Collect game schedules and results from ESPN's free public scoreboard API.

This is a starting source. Player-level stats and deeper history can be added
later, from paid feeds if the free data proves too thin.
"""

import logging

import requests

log = logging.getLogger(__name__)

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard"


def _score(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_scoreboard(league: str, data: dict) -> list[dict]:
    games = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        home, away = teams.get("home", {}), teams.get("away", {})
        games.append(
            {
                "game_id": str(event.get("id")),
                "league": league,
                "start_time": event.get("date"),
                "status": ((event.get("status") or {}).get("type") or {}).get("name"),
                "home_team": (home.get("team") or {}).get("displayName"),
                "away_team": (away.get("team") or {}).get("displayName"),
                "home_score": _score(home.get("score")),
                "away_score": _score(away.get("score")),
                "raw": event,
            }
        )
    return games


def fetch_scoreboard(league: str, date: str | None = None, session=None, timeout: float = 15.0) -> list[dict]:
    """league is ESPN's 'sport/league' path, e.g. 'football/nfl'. date is YYYYMMDD."""
    http = session or requests
    resp = http.get(
        ESPN_SCOREBOARD.format(league=league),
        params={"dates": date} if date else None,
        timeout=timeout,
    )
    resp.raise_for_status()
    return parse_scoreboard(league, resp.json())

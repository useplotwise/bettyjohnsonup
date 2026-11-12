"""Live temperature readings from the thermometers Kalshi settles on (free, no key).

Kalshi's daily high and low markets settle on the National Weather Service's
Daily Climate Report for one station per city (an airport, or Central Park in
New York). That same station posts a reading every 5 to 60 minutes, and the NWS
publishes them. Once readings are in, part of the day is no longer a guess: if
Chicago has already hit 78F, the high can't end up at 75.

- Readings are fetched per city every REFRESH_MINUTES and stored in the database.
- The climate report uses local STANDARD time all year (midnight to midnight,
  never shifted for daylight saving), so "today" here is that day.
- Readings come in Celsius and are converted to Fahrenheit. Readings the NWS
  flagged as bad are dropped.
- To guard against a single faulty reading, the "confirmed" high is the
  second-warmest reading of the day (the low: the second-coldest).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from .storage import Store
from .weather import HEADERS, NWS, SERIES, parse_day, series_of

log = logging.getLogger(__name__)

REFRESH_MINUTES = 10
MIN_READINGS = 3  # readings needed today before any of them is trusted
MAX_AGE_MINUTES = 60  # stored readings older than this are ignored
BAD_QC = {"X", "Q", "B"}  # NWS quality flags: rejected, questioned, bad

# city -> (station Kalshi settles on, UTC offset of local standard time)
STATIONS = {
    "nyc": ("KNYC", -5), "dc": ("KDCA", -5), "chi": ("KMDW", -6), "satx": ("KSAT", -6),
    "aus": ("KAUS", -6), "san": ("KSAN", -8), "min": ("KMSP", -6), "dal": ("KDFW", -6),
    "sea": ("KSEA", -8), "bos": ("KBOS", -5), "nola": ("KMSY", -6), "ewr": ("KEWR", -5),
    "den": ("KDEN", -7), "mia": ("KMIA", -5), "phil": ("KPHL", -5), "sfo": ("KSFO", -8),
    "atl": ("KATL", -5), "okc": ("KOKC", -6),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_observations (
    city TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    temps TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def local_day(city: str, now: datetime | None = None) -> tuple[str, datetime]:
    """(today's date in the city's local standard time, when that day started in UTC)."""
    now = now or datetime.now(timezone.utc)
    offset = timedelta(hours=STATIONS[city][1])
    local = now + offset
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.strftime("%Y-%m-%d"), (start_local - offset).replace(tzinfo=timezone.utc)


def to_fahrenheit(value: float, unit: str) -> float:
    return value if unit.endswith("degF") else value * 9 / 5 + 32


def parse_readings(features: list, start: datetime) -> list[list]:
    """[[timestamp, temp F], ...] for good readings taken on the day that began at `start` (UTC)."""
    end = start + timedelta(days=1)
    out = []
    for feature in features:
        props = feature.get("properties") or {}
        temp = props.get("temperature") or {}
        value = temp.get("value")
        if value is None or temp.get("qualityControl") in BAD_QC:
            continue
        try:
            when = datetime.fromisoformat(props["timestamp"].replace("Z", "+00:00"))
            reading = round(to_fahrenheit(float(value), temp.get("unitCode", "degC")), 1)
        except (KeyError, TypeError, ValueError):
            continue
        if start <= when < end:
            out.append([when.isoformat(timespec="seconds"), reading])
    return sorted(out)


def refresh_observations(store: Store, cities=None, session=None, now: datetime | None = None,
                         refresh_minutes: float = REFRESH_MINUTES) -> dict:
    """Fetch today's readings for each city whose stored copy is older than refresh_minutes."""
    store.conn.executescript(SCHEMA)
    now = now or datetime.now(timezone.utc)
    stats = {"cities_fetched": 0, "failed": 0}
    cutoff = (now - timedelta(minutes=refresh_minutes)).isoformat(timespec="seconds")
    for city in cities or STATIONS:
        day, start = local_day(city, now)
        row = store.conn.execute(
            "SELECT day, fetched_at FROM weather_observations WHERE city = ?", (city,)).fetchone()
        if row and row["day"] == day and row["fetched_at"] >= cutoff:
            continue
        url = f"{NWS}/stations/{STATIONS[city][0]}/observations"
        try:
            resp = (session or requests).get(
                url, params={"start": start.strftime("%Y-%m-%dT%H:%M:%SZ")}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            temps = parse_readings(resp.json().get("features") or [], start)
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            log.warning("observations %s: %s", city, exc)
            stats["failed"] += 1
            continue
        with store.conn:
            store.conn.execute(
                "INSERT INTO weather_observations (city, day, temps, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(city) DO UPDATE SET day = excluded.day, temps = excluded.temps, "
                "fetched_at = excluded.fetched_at",
                (city, day, json.dumps(temps), now.isoformat(timespec="seconds")),
            )
        stats["cities_fetched"] += 1
    return stats


def decided_side(market: dict, kind: str, observed: float, margin: float) -> str | None:
    """The side ("yes" or "no") today's readings have already settled, or None if still open.

    For a high, the day can only end at least as warm as `observed`; for a low,
    at least as cold. A market counts as settled only when the reading is past
    its edge by `margin` degrees, to cover rounding and Celsius conversion.
    Kalshi settles on whole degrees: "greater" means above floor_strike,
    "less" means below cap_strike, "between" includes both ends.
    """
    strike = market.get("strike_type")
    try:
        floor = float(market["floor_strike"]) if market.get("floor_strike") is not None else None
        cap = float(market["cap_strike"]) if market.get("cap_strike") is not None else None
    except (TypeError, ValueError):
        return None
    if kind == "high":
        if strike == "greater" and floor is not None and observed >= floor + 0.5 + margin:
            return "yes"
        if strike == "less" and cap is not None and observed >= cap - 0.5 + margin:
            return "no"
        if strike == "between" and cap is not None and observed >= cap + 0.5 + margin:
            return "no"
    elif kind == "low":
        if strike == "greater" and floor is not None and observed <= floor + 0.5 - margin:
            return "no"
        if strike == "less" and cap is not None and observed <= cap - 0.5 - margin:
            return "yes"
        if strike == "between" and floor is not None and observed <= floor - 0.5 - margin:
            return "no"
    return None


class Observations:
    """Answers "have today's readings already settled this Kalshi temperature market?"."""

    def __init__(self, readings: dict[str, tuple[str, list]]):
        self.readings = readings  # city -> (local day, [[timestamp, temp F], ...])

    @classmethod
    def from_store(cls, store: Store, now: datetime | None = None) -> "Observations":
        store.conn.executescript(SCHEMA)
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(minutes=MAX_AGE_MINUTES)).isoformat(timespec="seconds")
        rows = store.conn.execute(
            "SELECT city, day, temps FROM weather_observations WHERE fetched_at >= ?", (since,))
        out = {}
        for r in rows:
            if r["city"] in STATIONS and r["day"] == local_day(r["city"], now)[0]:
                out[r["city"]] = (r["day"], json.loads(r["temps"]))
        return cls(out)

    def confirmed(self, city: str, kind: str) -> float | None:
        """Second-warmest (high) or second-coldest (low) reading today, or None if too few."""
        _, temps = self.readings.get(city, ("", []))
        if len(temps) < MIN_READINGS:
            return None
        values = sorted(t for _, t in temps)
        return values[-2] if kind == "high" else values[1]

    def check(self, market: dict, margin: float) -> tuple[str, float, str] | None:
        """(settled side, confirmed reading, "high"/"low") for one of today's markets, or None."""
        series = series_of(market)
        if not series:
            return None
        city, kind = SERIES[series]
        if city not in self.readings:
            return None
        day = parse_day(market.get("event_ticker") or "") or parse_day(market.get("ticker") or "")
        if day != self.readings[city][0]:
            return None  # only today's markets; other days aren't decided by today's readings
        observed = self.confirmed(city, kind)
        if observed is None:
            return None
        side = decided_side(market, kind, observed, margin)
        return None if side is None else (side, observed, kind)

"""Weather forecasts from the US National Weather Service (free, no key).

Kalshi lists daily high and low temperature markets for big US cities
("69 or above", "67 to 68", "60 or below"), settled from the NWS station
report. The bot compares the NWS hourly forecast with Kalshi's price.

- Forecasts are fetched per city every REFRESH_MINUTES and stored in the database.
- The forecast high (or low) for a calendar day is the warmest (coldest)
  hour in the hourly forecast. A day is only used if the forecast covers
  nearly all of it, so a day already under way is never guessed at.
- The real temperature is modelled as a bell curve around the forecast, wider
  the further ahead the day is (SIGMA). Kalshi settles on whole degrees, so a
  bucket's chance counts everything that rounds into it.
"""

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import requests

from .storage import Store, utcnow

log = logging.getLogger(__name__)

NWS = "https://api.weather.gov"
HEADERS = {"User-Agent": "tradebot-paper-trading", "Accept": "application/geo+json"}
REFRESH_MINUTES = 90
MIN_HOURS = 23  # hourly forecast points needed for a day to count
# Typical forecast error (degrees F) by how many days ahead the day is.
SIGMA = {1: 2.5, 2: 3.0, 3: 3.5}

# series -> (city, "high" or "low")
SERIES = {
    "KXHIGHNY": ("nyc", "high"), "KXHIGHTDC": ("dc", "high"), "KXHIGHCHI": ("chi", "high"),
    "KXHIGHTSATX": ("satx", "high"), "KXHIGHAUS": ("aus", "high"), "KXHIGHTSAN": ("san", "high"),
    "KXHIGHTMIN": ("min", "high"), "KXHIGHTDAL": ("dal", "high"), "KXHIGHTSEA": ("sea", "high"),
    "KXHIGHTBOS": ("bos", "high"), "KXHIGHTNOLA": ("nola", "high"), "KXHIGHTEWR": ("ewr", "high"),
    "KXHIGHDEN": ("den", "high"),
    "KXLOWTSEA": ("sea", "low"), "KXLOWTSAN": ("san", "low"), "KXLOWTMIA": ("mia", "low"),
    "KXLOWTEWR": ("ewr", "low"), "KXLOWTPHIL": ("phil", "low"), "KXLOWTDEN": ("den", "low"),
    "KXLOWTAUS": ("aus", "low"), "KXLOWTMIN": ("min", "low"), "KXLOWTSFO": ("sfo", "low"),
    "KXLOWTATL": ("atl", "low"), "KXLOWTCHI": ("chi", "low"), "KXLOWTOKC": ("okc", "low"),
}
# city -> (latitude, longitude) of the airport / park station Kalshi settles on
CITIES = {
    "nyc": (40.7789, -73.9692), "dc": (38.8512, -77.0402), "chi": (41.7868, -87.7522),
    "satx": (29.5337, -98.4698), "aus": (30.1945, -97.6699), "san": (32.7336, -117.1897),
    "min": (44.8848, -93.2223), "dal": (32.8998, -97.0403), "sea": (47.4502, -122.3088),
    "bos": (42.3656, -71.0096), "nola": (29.9934, -90.2580), "ewr": (40.6895, -74.1745),
    "den": (39.8561, -104.6737), "mia": (25.7959, -80.2870), "phil": (39.8721, -75.2411),
    "sfo": (37.6213, -122.3790), "atl": (33.6407, -84.4277), "okc": (35.3931, -97.6007),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_forecasts (
    city TEXT PRIMARY KEY,
    hourly_url TEXT,
    temps TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_day(ticker: str) -> str | None:
    """"KXHIGHNY-26NOV10-T68" -> "2026-11-10"."""
    m = _DATE.search(ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    return f"{2000 + int(m.group(1))}-{_MONTHS[m.group(2)]:02d}-{int(m.group(3)):02d}"


def series_of(market: dict) -> str | None:
    event = market.get("event_ticker") or market.get("ticker") or ""
    series = event.split("-")[0]
    return series if series in SERIES else None


# -- fetching ---------------------------------------------------------------

def _get(session, url: str) -> dict:
    resp = (session or requests).get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def refresh_forecasts(store: Store, cities=None, session=None, refresh_minutes: float = REFRESH_MINUTES) -> dict:
    """Fetch the hourly forecast for each city whose stored copy is older than refresh_minutes."""
    store.conn.executescript(SCHEMA)
    stats = {"cities_fetched": 0, "failed": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=refresh_minutes)).isoformat(timespec="seconds")
    for city in cities or CITIES:
        row = store.conn.execute(
            "SELECT hourly_url, fetched_at FROM weather_forecasts WHERE city = ?", (city,)).fetchone()
        if row and row["fetched_at"] >= cutoff:
            continue
        try:
            url = row["hourly_url"] if row and row["hourly_url"] else None
            if not url:
                lat, lon = CITIES[city]
                url = _get(session, f"{NWS}/points/{lat},{lon}")["properties"]["forecastHourly"]
            periods = _get(session, url)["properties"]["periods"]
            temps = [[p["startTime"], p["temperature"]] for p in periods
                     if p.get("temperature") is not None and p.get("temperatureUnit", "F") == "F"]
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            log.warning("weather %s: %s", city, exc)
            stats["failed"] += 1
            continue
        with store.conn:
            store.conn.execute(
                "INSERT INTO weather_forecasts (city, hourly_url, temps, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(city) DO UPDATE SET hourly_url = excluded.hourly_url, temps = excluded.temps, "
                "fetched_at = excluded.fetched_at",
                (city, url, json.dumps(temps), utcnow()),
            )
        stats["cities_fetched"] += 1
    return stats


# -- turning a forecast into probabilities ------------------------------------

def _cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def bucket_prob(market: dict, mu: float, sigma: float) -> float | None:
    """Chance a Kalshi temperature market resolves YES, if the day's value is ~Normal(mu, sigma).

    Whole degrees settle the market, so "69 or above" means 68.5 or more.
    """
    kind = market.get("strike_type")
    floor, cap = market.get("floor_strike"), market.get("cap_strike")
    try:
        if kind == "greater" and floor is not None:
            return 1 - _cdf(float(floor) + 0.5, mu, sigma)
        if kind == "less" and cap is not None:
            return _cdf(float(cap) - 0.5, mu, sigma)
        if kind == "between" and floor is not None and cap is not None:
            return _cdf(float(cap) + 0.5, mu, sigma) - _cdf(float(floor) - 0.5, mu, sigma)
    except (TypeError, ValueError):
        pass
    return None


class Forecasts:
    """Answers "what chance does the NWS forecast give this Kalshi temperature market?"."""

    def __init__(self, hourly: dict[str, list]):
        self.hourly = hourly
        self._days: dict[tuple[str, str], tuple[float, float, int] | None] = {}

    @classmethod
    def from_store(cls, store: Store, max_age_hours: float = 6) -> "Forecasts":
        store.conn.executescript(SCHEMA)
        since = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        rows = store.conn.execute("SELECT city, temps FROM weather_forecasts WHERE fetched_at >= ?", (since,))
        return cls({r["city"]: json.loads(r["temps"]) for r in rows})

    def day_value(self, city: str, kind: str, day: str) -> tuple[float, int] | None:
        """(forecast high or low, days ahead) for a local calendar day, or None."""
        temps = self.hourly.get(city)
        if not temps:
            return None
        first = temps[0][0][:10]
        ahead = (datetime.fromisoformat(day) - datetime.fromisoformat(first)).days
        if ahead not in SIGMA:  # today (partly past) or too far ahead
            return None
        vals = [t for start, t in temps if start[:10] == day]
        if len(vals) < MIN_HOURS:
            return None
        return float(max(vals) if kind == "high" else min(vals)), ahead

    def yes_prob(self, market: dict) -> float | None:
        series = series_of(market)
        day = parse_day(market.get("event_ticker") or "") or parse_day(market.get("ticker") or "")
        if not series or not day:
            return None
        city, kind = SERIES[series]
        value = self.day_value(city, kind, day)
        if value is None:
            return None
        mu, ahead = value
        return bucket_prob(market, mu, SIGMA[ahead])

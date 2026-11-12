from datetime import datetime, timedelta, timezone

from tradebot.config import Settings
from tradebot.observations import (Observations, decided_side, local_day, parse_readings,
                                   refresh_observations)
from tradebot.storage import Store
from tradebot.strategy.runner import bankroll_rules
from tradebot.strategy.weather_today import find_today_bets

SOON = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
NOW = datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)  # 3 PM in Chicago (standard time)


def _m(ticker, name, kind, floor, cap, yes_ask, yes_bid):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": name, "yes_sub_title": name,
            "status": "active", "strike_type": kind, "floor_strike": floor, "cap_strike": cap,
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}", "close_time": SOON}


def _store(markets):
    store = Store(":memory:")
    store.upsert_markets([{"ticker": m["ticker"], "event_ticker": m["event_ticker"], "series_ticker": None,
                           "category": None, "title": m["title"], "status": "active", "is_combo": 0,
                           "close_time": SOON, "result": None, "raw": m} for m in markets])
    return store


def _readings(*temps):
    return [[f"2026-11-12T{12 + i:02d}:00:00+00:00", t] for i, t in enumerate(temps)]


def test_local_day_uses_standard_time():
    day, start = local_day("chi", datetime(2026, 11, 12, 5, 30, tzinfo=timezone.utc))
    assert day == "2026-11-11" and start == datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
    # Summer too: the climate report never shifts for daylight saving.
    day, start = local_day("nyc", datetime(2026, 7, 4, 4, 30, tzinfo=timezone.utc))
    assert day == "2026-07-03" and start == datetime(2026, 7, 3, 5, 0, tzinfo=timezone.utc)


def test_parse_readings_converts_and_filters():
    start = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)

    def f(ts, value, qc="V", unit="wmoUnit:degC"):
        return {"properties": {"timestamp": ts, "temperature": {"value": value, "unitCode": unit, "qualityControl": qc}}}

    out = parse_readings([
        f("2026-11-12T15:00:00+00:00", 20.0),
        f("2026-11-12T14:00:00+00:00", 10.0),
        f("2026-11-12T16:00:00+00:00", 30.0, qc="X"),  # flagged bad
        f("2026-11-12T17:00:00+00:00", None),  # missing
        f("2026-11-12T05:00:00+00:00", 15.0),  # yesterday, local time
        f("2026-11-12T18:00:00+00:00", 71.0, unit="wmoUnit:degF"),
    ], start)
    assert [t for _, t in out] == [50.0, 68.0, 71.0]


def test_decided_side_rules():
    def m(kind, floor=None, cap=None):
        return {"strike_type": kind, "floor_strike": floor, "cap_strike": cap}

    # High so far 78F (margin 1.5).
    assert decided_side(m("greater", floor=76), "high", 78, 1.5) == "yes"   # "77 or above": hit
    assert decided_side(m("greater", floor=77), "high", 78, 1.5) is None    # "78 or above": too close to call
    assert decided_side(m("less", cap=76), "high", 78, 1.5) == "no"         # "75 or below": dead
    assert decided_side(m("between", floor=74, cap=75), "high", 78, 1.5) == "no"
    assert decided_side(m("between", floor=76, cap=77), "high", 78, 1.5) is None
    # Low so far 40F.
    assert decided_side(m("greater", floor=41), "low", 40, 1.5) == "no"     # "42 or above": dead
    assert decided_side(m("less", cap=42), "low", 40, 1.5) == "yes"         # "41 or below": hit
    assert decided_side(m("between", floor=42, cap=43), "low", 40, 1.5) == "no"
    assert decided_side(m("between", floor=40, cap=41), "low", 40, 1.5) is None


def test_one_spike_is_not_trusted():
    obs = Observations({"chi": ("2026-11-12", _readings(60, 62, 90))})
    assert obs.confirmed("chi", "high") == 62
    assert obs.confirmed("chi", "low") == 62
    assert Observations({"chi": ("2026-11-12", _readings(60, 62))}).confirmed("chi", "high") is None


def test_bets_the_best_settled_market_once_per_day(tmp_path):
    obs = Observations({"chi": ("2026-11-12", _readings(70, 74, 78, 78.4))})
    store = _store([
        _m("KXHIGHCHI-26NOV12-T76", "77 or above", "greater", 76, None, yes_ask=0.85, yes_bid=0.83),  # settled YES
        _m("KXHIGHCHI-26NOV12-T75", "75 or below", "less", None, 76, yes_ask=0.12, yes_bid=0.10),  # settled NO, 0.90
        _m("KXHIGHCHI-26NOV12-B74.5", "74 to 75", "between", 74, 75, yes_ask=0.04, yes_bid=0.03),  # NO at 0.97: priced in
        _m("KXHIGHCHI-26NOV12-B76.5", "76 to 77", "between", 76, 77, yes_ask=0.30, yes_bid=0.28),  # still open
        _m("KXHIGHCHI-26NOV13-T76", "77 or above", "greater", 76, None, yes_ask=0.50, yes_bid=0.48),  # tomorrow
    ])
    settings = Settings(log_dir=tmp_path)
    stats = find_today_bets(settings, store, obs, bankroll_rules(settings), 0)
    assert stats == {"markets_settled_early": 3, "already_priced_in": 1, "today_bets": 1}
    row = store.conn.execute("SELECT * FROM paper_bets").fetchone()
    assert (row["kind"], row["ticker"], row["side"], row["price"]) == ("weather_today", "KXHIGHCHI-26NOV12-T76", "yes", 0.85)
    assert row["price"] * row["contracts"] <= 5.0  # stays within the starting $2 to $5
    # Never a second bet on the same city-day, even on another settled market.
    assert find_today_bets(settings, store, obs, bankroll_rules(settings), 0)["today_bets"] == 0


def test_no_readings_no_bets(tmp_path):
    store = _store([_m("KXHIGHCHI-26NOV12-T76", "77 or above", "greater", 76, None, yes_ask=0.5, yes_bid=0.48)])
    settings = Settings(log_dir=tmp_path)
    assert find_today_bets(settings, store, Observations({}), bankroll_rules(settings), 0)["today_bets"] == 0


class FakeNWS:
    def __init__(self):
        self.calls = []

    def get(self, url, params, headers, timeout):
        self.calls.append((url, params))

        class Resp:
            def raise_for_status(self):
                pass

            def json(_):
                return {"features": [
                    {"properties": {"timestamp": f"2026-11-12T{h:02d}:00:00+00:00",
                                    "temperature": {"value": 20.0 + h / 10, "unitCode": "wmoUnit:degC",
                                                    "qualityControl": "V"}}}
                    for h in range(7, 21)]}
        return Resp()


def test_refresh_is_cached_and_read_back():
    store = Store(":memory:")
    session = FakeNWS()
    assert refresh_observations(store, ["chi"], session, now=NOW) == {"cities_fetched": 1, "failed": 0}
    assert refresh_observations(store, ["chi"], session, now=NOW + timedelta(minutes=5))["cities_fetched"] == 0
    assert session.calls == [("https://api.weather.gov/stations/KMDW/observations", {"start": "2026-11-12T06:00:00Z"})]
    obs = Observations.from_store(store, now=NOW)
    assert obs.readings["chi"][0] == "2026-11-12" and len(obs.readings["chi"][1]) == 14
    # Stale readings are ignored.
    assert Observations.from_store(store, now=NOW + timedelta(hours=2)).readings == {}

from datetime import datetime, timedelta, timezone

from tradebot.config import Settings
from tradebot.storage import Store
from tradebot.strategy.runner import bankroll_rules
from tradebot.strategy.weather_bets import find_weather_bets
from tradebot.weather import Forecasts, bucket_prob, parse_day, refresh_forecasts

SOON = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
TODAY = datetime(2026, 11, 9)


def _hourly(high=68, low=50, days=3):
    out = []
    for d in range(days):
        for h in range(24):
            day = (TODAY + timedelta(days=d)).strftime("%Y-%m-%d")
            out.append([f"{day}T{h:02d}:00:00-05:00", high if h == 15 else low + (h % 5)])
    return out


def _m(ticker, name, kind, floor, cap, ask=0.5, bid=0.48):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": name, "yes_sub_title": name,
            "status": "active", "strike_type": kind, "floor_strike": floor, "cap_strike": cap,
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "close_time": SOON}


def test_parse_day_and_buckets():
    assert parse_day("KXHIGHNY-26NOV10-T68") == "2026-11-10"
    assert parse_day("nonsense") is None
    greater = bucket_prob({"strike_type": "greater", "floor_strike": 68}, 70, 2.5)
    less = bucket_prob({"strike_type": "less", "cap_strike": 61}, 70, 2.5)
    assert greater > 0.7 and less < 0.001
    # The whole partition of a day adds up to 1.
    parts = [bucket_prob({"strike_type": "between", "floor_strike": f, "cap_strike": f + 1}, 67.2, 2.5)
             for f in range(61, 68, 2)]
    parts += [bucket_prob({"strike_type": "less", "cap_strike": 61}, 67.2, 2.5),
              bucket_prob({"strike_type": "greater", "floor_strike": 68}, 67.2, 2.5)]
    assert abs(sum(parts) - 1) < 1e-9


def test_forecast_day_rules():
    f = Forecasts({"nyc": _hourly(high=68)})
    assert f.day_value("nyc", "high", "2026-11-10") == (68.0, 1)
    assert f.day_value("nyc", "low", "2026-11-10") == (50.0, 1)
    assert f.day_value("nyc", "high", "2026-11-09") is None  # today: partly past
    assert f.day_value("nyc", "high", "2026-11-14") is None  # too far ahead
    short = Forecasts({"nyc": _hourly()[:30]})
    assert short.day_value("nyc", "high", "2026-11-10") is None  # not enough hours


class FakeNWS:
    def __init__(self):
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append(url)

        class Resp:
            def raise_for_status(self):
                pass

            def json(_):
                if "/points/" in url:
                    return {"properties": {"forecastHourly": "https://nws.test/hourly"}}
                return {"properties": {"periods": [
                    {"startTime": s, "temperature": t, "temperatureUnit": "F"} for s, t in _hourly()]}}
        return Resp()


def test_refresh_is_cached():
    store = Store(":memory:")
    session = FakeNWS()
    assert refresh_forecasts(store, ["nyc"], session) == {"cities_fetched": 1, "failed": 0}
    assert refresh_forecasts(store, ["nyc"], session)["cities_fetched"] == 0
    assert len(session.calls) == 2  # point lookup + forecast, once
    assert Forecasts.from_store(store).day_value("nyc", "high", "2026-11-10") == (68.0, 1)


def _store(markets):
    store = Store(":memory:")
    store.upsert_markets([{"ticker": m["ticker"], "event_ticker": m["event_ticker"], "series_ticker": None,
                           "category": None, "title": m["title"], "status": "active", "is_combo": 0,
                           "close_time": SOON, "result": None, "raw": m} for m in markets])
    return store


def test_best_bucket_is_bet_once_per_day(tmp_path):
    f = Forecasts({"nyc": _hourly(high=68)})
    markets = [
        _m("KXHIGHNY-26NOV10-B67.5", "67 to 68", "between", 67, 68, ask=0.30, bid=0.28),  # forecast ~41%
        _m("KXHIGHNY-26NOV10-T68", "69 or above", "greater", 68, None, ask=0.28, bid=0.26),  # forecast ~39%
        _m("KXHIGHNY-26NOV10-T61", "60 or below", "less", None, 61, ask=0.50, bid=0.48),  # forecast ~0%: gap rejected
        _m("KXHIGHNY-26NOV12-T68", "69 or above", "greater", 68, None, ask=0.33, bid=0.31),  # 3 days ahead, ok
        _m("KXHIGHBOS-26NOV10-T68", "69 or above", "greater", 68, None),  # series unknown: ignored
    ]
    store = _store(markets)
    settings = Settings(log_dir=tmp_path)
    stats = find_weather_bets(settings, store, f, bankroll_rules(settings), 0)
    assert stats["rejected"] == 1 and stats["weather_bets"] >= 1
    rows = store.conn.execute("SELECT ticker, kind, side FROM paper_bets").fetchall()
    assert all(r["kind"] == "weather" for r in rows)
    events = [r["ticker"].rsplit("-", 1)[0] for r in rows]
    assert len(events) == len(set(events))  # one bet per city-day
    assert find_weather_bets(settings, store, f, bankroll_rules(settings), 0)["weather_bets"] == 0


def test_no_forecast_no_bets(tmp_path):
    store = _store([_m("KXHIGHNY-26NOV10-T68", "69 or above", "greater", 68, None)])
    settings = Settings(log_dir=tmp_path)
    assert find_weather_bets(settings, store, Forecasts({}), bankroll_rules(settings), 0)["weather_bets"] == 0


def test_cheap_long_shot_tails_are_not_bet(tmp_path):
    """The first real results: buying 5-15 cent tails the old wide model liked lost 23 of 25 times."""
    f = Forecasts({"nyc": _hourly(high=68)})
    store = _store([
        _m("KXHIGHNY-26NOV10-T70", "71 or above", "greater", 70, None, ask=0.10, bid=0.08),
        _m("KXHIGHNY-26NOV11-T70", "71 or above", "greater", 70, None, ask=0.14, bid=0.12),
    ])
    settings = Settings(log_dir=tmp_path)
    assert find_weather_bets(settings, store, f, bankroll_rules(settings), 0)["weather_bets"] == 0

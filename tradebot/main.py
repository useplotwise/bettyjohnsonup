"""Entry point: run one collection cycle, or loop on a schedule.

    python -m tradebot.main --once      # single cycle, then exit
    python -m tradebot.main             # loop every LOOP_INTERVAL_MINUTES

Real bets are placed only when DRY_RUN=false and BOT_BANKROLL is above 0.
"""

import argparse
import logging
import time
from logging.handlers import TimedRotatingFileHandler

import requests

from .collectors.markets import collect_markets, collect_orderbooks
from .collectors.sports import fetch_scoreboard
from .config import Settings, load_settings
from .kalshi_client import KalshiClient, load_private_key
from .live import bot_ledger, live_enabled
from .odds import BookOdds, refresh_odds
from .strategy.weather_bets import find_weather_bets
from .strategy.weather_today import find_today_bets
from .observations import Observations, refresh_observations
from .weather import Forecasts, refresh_forecasts
from .paper import settle_paper_bets
from .scorecard import write_scorecard
from .storage import Store
from .strategy.cashout import PaperCashOuts, live_cash_outs
from .strategy.runner import LegLookup, bankroll_rules, check_cash_outs, evaluate_combos
from .strategy.value import find_value_bets

log = logging.getLogger("tradebot")


def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = TimedRotatingFileHandler(
        settings.log_dir / "tradebot.log", when="midnight", backupCount=30, utc=True
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]


def build_client(settings: Settings) -> KalshiClient:
    key = load_private_key(settings.kalshi_private_key_path) if settings.has_credentials else None
    return KalshiClient(settings.kalshi_base_url, settings.kalshi_api_key_id, key)


def run_cycle(settings: Settings, client: KalshiClient, store: Store) -> dict:
    run_id = store.start_run(settings.dry_run)
    summary: dict = {"env": settings.kalshi_env, "dry_run": settings.dry_run, "errors": []}

    log.info("scan started (%s exchange, dry run: %s)", settings.kalshi_env, settings.dry_run)
    try:
        markets = collect_markets(
            client, store, settings.max_markets_per_scan, settings.max_combos_per_scan,
            settings.kalshi_game_series + settings.kalshi_weather_series,
        )
        summary["markets"] = len(markets)
        summary["combos"] = sum(m["is_combo"] for m in markets)
        # Split order book reads between combos (priority 1) and everything else.
        half = settings.max_orderbooks_per_scan // 2
        combo_tickers = [m["ticker"] for m in markets if m["is_combo"]]
        other_tickers = [m["ticker"] for m in markets if not m["is_combo"]]
        tickers = combo_tickers[:half] + other_tickers
        tickers += combo_tickers[half:]
        summary["orderbooks"] = collect_orderbooks(
            client, store, tickers, settings.max_orderbooks_per_scan
        )
    except Exception as exc:  # keep the loop alive; the next cycle retries
        log.exception("market collection failed")
        summary["errors"].append(f"markets: {exc}")

    games = 0
    log.info("reading sports results for %d leagues...", len(settings.sports_leagues))
    for league in settings.sports_leagues:
        try:
            games += store.upsert_games(fetch_scoreboard(league))
        except requests.RequestException as exc:
            log.warning("scoreboard %s: %s", league, exc)
            summary["errors"].append(f"sports {league}: {exc}")
    summary["games"] = games

    balance = None
    if settings.has_credentials:
        try:
            summary["balance"] = client.get_balance().get("balance")
            balance = summary["balance"] / 100 if summary["balance"] is not None else None
        except Exception as exc:
            summary["errors"].append(f"balance: {exc}")

    # Bet sizes grow only with the bot's own settled real-money profit; in dry
    # run (and before any real bet settles) sizing stays at $2 to $5.
    lifetime_profit = bot_ledger(store)["profit"] if live_enabled(settings) else 0.0
    # Paper bets aren't limited by the real balance; only real bets are.
    bet_balance = balance if live_enabled(settings) else None

    try:
        summary["odds"] = refresh_odds(
            store, settings.odds_api_key, settings.odds_sports, settings.odds_regions,
            settings.odds_refresh_minutes,
        )
    except Exception as exc:
        log.exception("odds step failed")
        summary["errors"].append(f"odds: {exc}")
    book = BookOdds.from_store(store)
    try:
        summary["weather_forecasts"] = refresh_forecasts(store)
    except Exception as exc:
        log.exception("weather forecast step failed")
        summary["errors"].append(f"weather forecasts: {exc}")
    forecasts = Forecasts.from_store(store)
    try:
        summary["weather_readings"] = refresh_observations(store)
    except Exception as exc:
        log.exception("weather readings step failed")
        summary["errors"].append(f"weather readings: {exc}")
    observations = Observations.from_store(store)
    lookup = LegLookup(client, store, settings.max_leg_lookups)

    for name, step in (
        ("combo_picks", lambda: evaluate_combos(settings, client, store, bet_balance, lifetime_profit, book)),
        ("value_bets", lambda: find_value_bets(settings, store, book, bankroll_rules(settings), lifetime_profit,
                                              client, bet_balance)),
        ("weather_bets", lambda: find_weather_bets(settings, store, forecasts, bankroll_rules(settings),
                                                   lifetime_profit)),
        ("today_bets", lambda: find_today_bets(settings, store, observations, bankroll_rules(settings),
                                               lifetime_profit)),
        ("cashouts", lambda: check_cash_outs(settings, client, store)),
        ("live_cashouts", lambda: live_cash_outs(settings, client, store, lookup, book.leg_prob)),
        ("paper", lambda: settle_paper_bets(client, store, on_open=PaperCashOuts(store, lookup, book.leg_prob))),
    ):
        try:
            summary[name] = step()
        except Exception as exc:
            log.exception("%s step failed", name)
            summary["errors"].append(f"{name}: {exc}")
    summary["sportsbook_matches"] = book.matched

    try:
        summary["scorecard"] = str(write_scorecard(store, settings.log_dir, balance, settings.dry_run, settings))
    except Exception as exc:
        log.exception("scorecard failed")
        summary["errors"].append(f"scorecard: {exc}")

    try:
        summary["cleanup"] = store.prune()
    except Exception as exc:
        log.exception("cleanup failed")
        summary["errors"].append(f"cleanup: {exc}")

    store.finish_run(run_id, summary)
    log.info("cycle done: %s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Prediction-market trading bot")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings)
    if live_enabled(settings):
        log.warning("LIVE: real bets are on, using up to $%.2f of bot money.", settings.bot_bankroll)
    elif not settings.dry_run:
        log.warning("DRY_RUN is off but BOT_BANKROLL is 0, so nothing will trade.")
    client = build_client(settings)
    store = Store(settings.db_path)

    try:
        while True:
            started = time.monotonic()
            run_cycle(settings, client, store)
            if args.once:
                break
            time.sleep(max(0.0, settings.loop_interval_minutes * 60 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        store.close()


if __name__ == "__main__":
    main()

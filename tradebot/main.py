"""Entry point: run one collection cycle, or loop on a schedule.

    python -m tradebot.main --once      # single cycle, then exit
    python -m tradebot.main             # loop every LOOP_INTERVAL_MINUTES

No orders are placed by this code yet; order placement arrives in Phase 4.
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
from .paper import settle_paper_bets
from .scorecard import write_scorecard
from .storage import Store
from .strategy.runner import check_cash_outs, evaluate_combos

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
            client, store, settings.max_markets_per_scan, settings.max_combos_per_scan
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

    # Lifetime profit tracking starts with live trading (Phase 4); until then
    # sizing stays at the $2 to $5 starting range.
    lifetime_profit = 0.0
    for name, step in (
        ("combo_picks", lambda: evaluate_combos(settings, client, store, balance, lifetime_profit)),
        ("cashouts", lambda: check_cash_outs(settings, client, store)),
        ("paper", lambda: settle_paper_bets(client, store)),
    ):
        try:
            summary[name] = step()
        except Exception as exc:
            log.exception("%s step failed", name)
            summary["errors"].append(f"{name}: {exc}")

    try:
        summary["scorecard"] = str(write_scorecard(store, settings.log_dir, balance, settings.dry_run))
    except Exception as exc:
        log.exception("scorecard failed")
        summary["errors"].append(f"scorecard: {exc}")

    store.finish_run(run_id, summary)
    log.info("cycle done: %s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Prediction-market trading bot")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings)
    if not settings.dry_run:
        log.warning("DRY_RUN is off, but order placement arrives in Phase 4; nothing will trade.")
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

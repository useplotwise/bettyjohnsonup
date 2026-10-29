"""Entry point: run one collection cycle, or loop on a schedule.

    python -m tradebot.main --once      # single cycle, then exit
    python -m tradebot.main             # loop every LOOP_INTERVAL_MINUTES

Step 1 only collects data. No orders are placed by this code.
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
from .storage import Store

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

    try:
        markets = collect_markets(client, store)
        summary["markets"] = len(markets)
        summary["combos"] = sum(m["is_combo"] for m in markets)
        # Combos first (priority 1), then everything else.
        tickers = [m["ticker"] for m in sorted(markets, key=lambda m: -m["is_combo"])]
        summary["orderbooks"] = collect_orderbooks(client, store, tickers)
    except Exception as exc:  # keep the loop alive; the next cycle retries
        log.exception("market collection failed")
        summary["errors"].append(f"markets: {exc}")

    games = 0
    for league in settings.sports_leagues:
        try:
            games += store.upsert_games(fetch_scoreboard(league))
        except requests.RequestException as exc:
            log.warning("scoreboard %s: %s", league, exc)
            summary["errors"].append(f"sports {league}: {exc}")
    summary["games"] = games

    if settings.has_credentials:
        try:
            summary["balance"] = client.get_balance().get("balance")
        except Exception as exc:
            summary["errors"].append(f"balance: {exc}")

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
        log.warning("DRY_RUN is off, but step 1 has no order placement; nothing will trade.")
    client = build_client(settings)
    store = Store(settings.db_path)

    while True:
        started = time.monotonic()
        run_cycle(settings, client, store)
        if args.once:
            break
        time.sleep(max(0.0, settings.loop_interval_minutes * 60 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()

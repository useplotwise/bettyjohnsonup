"""Settings loaded from environment variables (or a .env file)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .weather import SERIES

KALSHI_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    kalshi_env: str = "demo"
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    dry_run: bool = True
    loop_interval_minutes: float = 5.0
    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    max_bet_default: float = 20.0
    high_confidence_prob: float = 0.85
    manual_approval_above: float = 500.0
    sports_leagues: tuple[str, ...] = field(default_factory=tuple)
    max_markets_per_scan: int = 5000
    max_combos_per_scan: int = 2000
    max_orderbooks_per_scan: int = 100
    min_stake: float = 2.0
    start_max_stake: float = 5.0
    scale_fraction: float = 0.10
    min_edge: float = 0.05
    combos_to_evaluate: int = 40
    max_leg_lookups: int = 200
    rfq_checks_per_cycle: int = 1
    bot_bankroll: float = 0.0
    max_live_bets_per_day: int = 5
    max_daily_loss_fraction: float = 0.25
    max_drawdown_fraction: float = 0.5
    builds_per_cycle: int = 1
    builder_collection: str = "KXMVECROSSCATEGORY-R"
    builder_leg_counts: tuple[int, ...] = (2, 3, 4, 6, 8)
    builder_min_leg_prob: float = 0.65
    builder_max_leg_prob: float = 0.95
    builder_max_spread: float = 0.06
    builder_longshot_leg_counts: tuple[int, ...] = (3, 4, 5)
    builder_longshot_min_prob: float = 0.20
    builder_longshot_max_prob: float = 0.50
    odds_api_key: str = ""
    odds_sports: tuple[str, ...] = (
        "basketball_nba", "americanfootball_nfl", "icehockey_nhl",
        "americanfootball_ncaaf", "basketball_ncaab", "soccer_epl",
    )
    odds_regions: str = "eu"
    odds_refresh_minutes: float = 720.0
    # Kalshi game-winner series, read in full every scan so the sportsbook
    # odds have Kalshi markets to compare against.
    kalshi_game_series: tuple[str, ...] = (
        "KXNBAGAME", "KXNFLGAME", "KXNHLGAME", "KXNCAAFGAME", "KXNCAAMBGAME", "KXEPLGAME",
    )
    # Daily temperature series (high and low) read in full every scan.
    kalshi_weather_series: tuple[str, ...] = tuple(SERIES)

    @property
    def kalshi_base_url(self) -> str:
        return KALSHI_BASE_URLS[self.kalshi_env]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tradebot.sqlite3"

    @property
    def has_credentials(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file, override=False)
    env = os.getenv("KALSHI_ENV", "demo").strip().lower()
    if env not in KALSHI_BASE_URLS:
        raise ValueError(f"KALSHI_ENV must be one of {sorted(KALSHI_BASE_URLS)}, got {env!r}")
    leagues = tuple(
        s.strip() for s in os.getenv("SPORTS_LEAGUES", "").split(",") if s.strip()
    )
    return Settings(
        kalshi_env=env,
        kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        dry_run=_bool(os.getenv("DRY_RUN"), True),
        loop_interval_minutes=float(os.getenv("LOOP_INTERVAL_MINUTES", "5")),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
        log_dir=Path(os.getenv("LOG_DIR", "./logs")),
        max_bet_default=float(os.getenv("MAX_BET_DEFAULT", "20")),
        high_confidence_prob=float(os.getenv("HIGH_CONFIDENCE_PROB", "0.85")),
        manual_approval_above=float(os.getenv("MANUAL_APPROVAL_ABOVE", "500")),
        sports_leagues=leagues,
        max_markets_per_scan=int(os.getenv("MAX_MARKETS_PER_SCAN", "5000")),
        max_combos_per_scan=int(os.getenv("MAX_COMBOS_PER_SCAN", "2000")),
        max_orderbooks_per_scan=int(os.getenv("MAX_ORDERBOOKS_PER_SCAN", "100")),
        min_stake=float(os.getenv("MIN_STAKE", "2")),
        start_max_stake=float(os.getenv("START_MAX_STAKE", "5")),
        scale_fraction=float(os.getenv("SCALE_FRACTION", "0.10")),
        min_edge=float(os.getenv("MIN_EDGE", "0.05")),
        combos_to_evaluate=int(os.getenv("COMBOS_TO_EVALUATE", "40")),
        max_leg_lookups=int(os.getenv("MAX_LEG_LOOKUPS", "200")),
        rfq_checks_per_cycle=int(os.getenv("RFQ_CHECKS_PER_CYCLE", "1")),
        bot_bankroll=float(os.getenv("BOT_BANKROLL", "0")),
        max_live_bets_per_day=int(os.getenv("MAX_LIVE_BETS_PER_DAY", "5")),
        max_daily_loss_fraction=float(os.getenv("MAX_DAILY_LOSS_FRACTION", "0.25")),
        max_drawdown_fraction=float(os.getenv("MAX_DRAWDOWN_FRACTION", "0.5")),
        builds_per_cycle=int(os.getenv("BUILDS_PER_CYCLE", "1")),
        builder_collection=os.getenv("BUILDER_COLLECTION", "KXMVECROSSCATEGORY-R"),
        builder_leg_counts=tuple(
            int(n) for n in os.getenv("BUILDER_LEG_COUNTS", "2,3,4,6,8").split(",") if n.strip()
        ),
        builder_min_leg_prob=float(os.getenv("BUILDER_MIN_LEG_PROB", "0.65")),
        builder_max_leg_prob=float(os.getenv("BUILDER_MAX_LEG_PROB", "0.95")),
        builder_max_spread=float(os.getenv("BUILDER_MAX_SPREAD", "0.06")),
        builder_longshot_leg_counts=tuple(
            int(n) for n in os.getenv("BUILDER_LONGSHOT_LEG_COUNTS", "3,4,5").split(",") if n.strip()
        ),
        builder_longshot_min_prob=float(os.getenv("BUILDER_LONGSHOT_MIN_PROB", "0.20")),
        builder_longshot_max_prob=float(os.getenv("BUILDER_LONGSHOT_MAX_PROB", "0.50")),
        odds_api_key=os.getenv("ODDS_API_KEY", ""),
        odds_sports=tuple(
            s.strip() for s in os.getenv(
                "ODDS_SPORTS",
                "basketball_nba,americanfootball_nfl,icehockey_nhl,americanfootball_ncaaf,"
                "basketball_ncaab,soccer_epl",
            ).split(",") if s.strip()
        ),
        odds_regions=os.getenv("ODDS_REGIONS", "eu"),
        odds_refresh_minutes=float(os.getenv("ODDS_REFRESH_MINUTES", "720")),
        kalshi_weather_series=tuple(
            s.strip() for s in os.getenv("KALSHI_WEATHER_SERIES", ",".join(SERIES)).split(",") if s.strip()
        ),
        kalshi_game_series=tuple(
            s.strip() for s in os.getenv(
                "KALSHI_GAME_SERIES",
                "KXNBAGAME,KXNFLGAME,KXNHLGAME,KXNCAAFGAME,KXNCAAMBGAME,KXEPLGAME",
            ).split(",") if s.strip()
        ),
    )

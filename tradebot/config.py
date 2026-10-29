"""Settings loaded from environment variables (or a .env file)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

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
    )

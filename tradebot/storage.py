"""SQLite storage for market snapshots, order books and sports results."""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    category TEXT,
    title TEXT,
    status TEXT,
    is_combo INTEGER NOT NULL DEFAULT 0,
    close_time TEXT,
    result TEXT,
    raw TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_updated ON markets (updated_at);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    best_yes_bid INTEGER,
    best_no_bid INTEGER,
    yes_ask INTEGER,
    yes_depth INTEGER NOT NULL,
    no_depth INTEGER NOT NULL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ob_ticker_time ON orderbook_snapshots (ticker, taken_at);
CREATE INDEX IF NOT EXISTS idx_ob_time ON orderbook_snapshots (taken_at);

CREATE TABLE IF NOT EXISTS sports_games (
    game_id TEXT NOT NULL,
    league TEXT NOT NULL,
    start_time TEXT,
    status TEXT,
    home_team TEXT,
    away_team TEXT,
    home_score INTEGER,
    away_score INTEGER,
    raw TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (league, game_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT,
    action TEXT NOT NULL,
    win_prob REAL,
    price REAL,
    edge REAL,
    stake REAL,
    contracts INTEGER,
    dry_run INTEGER NOT NULL,
    reason TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions (made_at);

CREATE TABLE IF NOT EXISTS paper_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    contracts INTEGER NOT NULL,
    fee REAL NOT NULL,
    win_prob REAL,
    edge REAL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL,
    checked_at TEXT,
    settled_at TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_bets (status, checked_at);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL,
    summary TEXT
);
"""


log = logging.getLogger(__name__)

# How long old records are kept before the bot deletes them (see Store.prune).
KEEP_ORDERBOOK_DAYS = 2
KEEP_MARKET_DAYS = 3
# Kalshi lists about 2,000 brand-new combo markets every scan and the bot only
# looks at the newest ones, so old combos are dropped much sooner.
KEEP_COMBO_HOURS = 2
KEEP_RUN_LOG_DAYS = 30


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert_markets(self, rows: Iterable[dict]) -> int:
        now = utcnow()
        n = 0
        with self.conn:
            for r in rows:
                self.conn.execute(
                    """INSERT INTO markets (ticker, event_ticker, series_ticker, category, title,
                           status, is_combo, close_time, result, raw, updated_at)
                       VALUES (:ticker, :event_ticker, :series_ticker, :category, :title,
                           :status, :is_combo, :close_time, :result, :raw, :updated_at)
                       ON CONFLICT(ticker) DO UPDATE SET
                           status=excluded.status, result=excluded.result,
                           close_time=excluded.close_time, raw=excluded.raw,
                           updated_at=excluded.updated_at""",
                    {**r, "raw": json.dumps(r["raw"]), "updated_at": now},
                )
                n += 1
        return n

    def add_orderbook_snapshot(self, snap: dict) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO orderbook_snapshots (ticker, taken_at, best_yes_bid, best_no_bid,
                       yes_ask, yes_depth, no_depth, raw)
                   VALUES (:ticker, :taken_at, :best_yes_bid, :best_no_bid, :yes_ask,
                       :yes_depth, :no_depth, :raw)""",
                # The full order book isn't used later and is the biggest thing we
                # stored, so only the summary numbers are kept.
                {**snap, "raw": "{}"},
            )

    def upsert_games(self, rows: Iterable[dict]) -> int:
        now = utcnow()
        n = 0
        with self.conn:
            for r in rows:
                self.conn.execute(
                    """INSERT INTO sports_games (game_id, league, start_time, status, home_team,
                           away_team, home_score, away_score, raw, updated_at)
                       VALUES (:game_id, :league, :start_time, :status, :home_team,
                           :away_team, :home_score, :away_score, :raw, :updated_at)
                       ON CONFLICT(league, game_id) DO UPDATE SET
                           status=excluded.status, home_score=excluded.home_score,
                           away_score=excluded.away_score, raw=excluded.raw,
                           updated_at=excluded.updated_at""",
                    {**r, "raw": json.dumps(r["raw"]), "updated_at": now},
                )
                n += 1
        return n

    def start_run(self, dry_run: bool) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO run_log (started_at, dry_run) VALUES (?, ?)", (utcnow(), int(dry_run))
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE run_log SET finished_at = ?, summary = ? WHERE id = ?",
                (utcnow(), json.dumps(summary), run_id),
            )

    def add_decision(self, d: dict) -> None:
        row = {
            "side": None, "win_prob": None, "price": None, "edge": None, "stake": None,
            "contracts": None, "reason": None, "raw": None, **d,
        }
        row["raw"] = json.dumps(row["raw"]) if row["raw"] is not None else None
        with self.conn:
            self.conn.execute(
                """INSERT INTO decisions (made_at, kind, ticker, side, action, win_prob, price,
                       edge, stake, contracts, dry_run, reason, raw)
                   VALUES (:made_at, :kind, :ticker, :side, :action, :win_prob, :price,
                       :edge, :stake, :contracts, :dry_run, :reason, :raw)""",
                {"made_at": utcnow(), **row, "dry_run": int(row["dry_run"])},
            )

    def decisions_since(self, since_iso: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM decisions WHERE made_at >= ? ORDER BY made_at", (since_iso,)
        ).fetchall()

    def recent_decision(self, ticker: str, action: str, since_iso: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM decisions WHERE ticker = ? AND action = ? AND made_at >= ? LIMIT 1",
                (ticker, action, since_iso),
            ).fetchone()
            is not None
        )

    def get_market_raw(self, ticker: str) -> dict | None:
        row = self.conn.execute("SELECT raw FROM markets WHERE ticker = ?", (ticker,)).fetchone()
        return json.loads(row[0]) if row else None

    def combo_markets(self, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT raw FROM markets WHERE is_combo = 1 AND status IN ('open', 'active') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def leg_markets(self, limit: int = 20000, fresh_minutes: float | None = None) -> list[dict]:
        """Open single (non-combo) markets, usable as combo legs.

        With fresh_minutes, only markets the latest scans saw, so prices are current.
        """
        since = ""
        if fresh_minutes is not None:
            since = (datetime.now(timezone.utc) - timedelta(minutes=fresh_minutes)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT raw FROM markets WHERE is_combo = 0 AND status IN ('open', 'active') "
            "AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def prune(self) -> dict:
        """Delete old records so the database can't fill the disk.

        Keeps: every decision and paper bet (the learning history), sports
        results, and any market a paper bet still needs. Deletes order book
        snapshots older than KEEP_ORDERBOOK_DAYS, markets not seen for
        KEEP_MARKET_DAYS (combo markets: KEEP_COMBO_HOURS), and run summaries older than KEEP_RUN_LOG_DAYS.
        Freed space is reused by new rows, so the file stops growing.
        """
        now = datetime.now(timezone.utc)

        def ago(days: float = 0, hours: float = 0) -> str:
            return (now - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")

        keep_open_bets = "ticker NOT IN (SELECT ticker FROM paper_bets WHERE status = 'open')"

        with self.conn:
            books = self.conn.execute(
                "DELETE FROM orderbook_snapshots WHERE taken_at < ?", (ago(KEEP_ORDERBOOK_DAYS),)
            ).rowcount
            markets = self.conn.execute(
                f"DELETE FROM markets WHERE is_combo = 1 AND updated_at < ? AND {keep_open_bets}",
                (ago(hours=KEEP_COMBO_HOURS),),
            ).rowcount
            markets += self.conn.execute(
                f"DELETE FROM markets WHERE updated_at < ? AND {keep_open_bets}",
                (ago(KEEP_MARKET_DAYS),),
            ).rowcount
            runs = self.conn.execute(
                "DELETE FROM run_log WHERE started_at < ?", (ago(KEEP_RUN_LOG_DAYS),)
            ).rowcount
        if books or markets or runs:
            log.info("cleanup: removed %d old order books, %d old markets, %d old run logs",
                     books, markets, runs)
        return {"orderbooks": books, "markets": markets, "run_log": runs}

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

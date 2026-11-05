"""Shrink the bot's database when the disk is full.

    systemctl stop tradebot
    sudo -u tradebot bash -c 'cd /home/tradebot/bettyjohnsonup && .venv/bin/python -m tradebot.compact'
    systemctl start tradebot

Copies what matters into a fresh, small database file, then swaps it in:
every paper bet and decision (the learning history), sports results,
sportsbook odds, recent run summaries, and markets
still needed by an open paper bet (the next scan refills the rest). Old
order book snapshots are dropped.
The old file is replaced only after the copy succeeds; if space runs out,
nothing is changed.
"""

import os
import sqlite3
import sys
from pathlib import Path

from .config import load_settings
from .storage import KEEP_RUN_LOG_DAYS, Store

KEEP_ALL = ("paper_bets", "decisions", "sports_games", "sportsbook_odds", "odds_fetches")


def _mb(path: Path) -> float:
    return path.stat().st_size / 1e6 if path.exists() else 0.0


def compact(db_path: Path) -> Path:
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")
    new_path = db_path.with_suffix(".compact.sqlite3")
    if new_path.exists():
        new_path.unlink()
    print(f"database now: {_mb(db_path):,.0f} MB", flush=True)

    try:
        counts = _copy(db_path, new_path, lean=False)
    except sqlite3.OperationalError as exc:
        new_path.unlink(missing_ok=True)
        if "full" not in str(exc):
            raise
        print("not enough room for a full copy; trying again without old decision details...", flush=True)
        try:
            counts = _copy(db_path, new_path, lean=True)
        except sqlite3.OperationalError:
            new_path.unlink(missing_ok=True)
            raise SystemExit("Still not enough free space. Nothing was changed. Ask Claude for help.")

    os.replace(new_path, db_path)  # the old file's space is freed here
    for extra in ("-wal", "-shm", "-journal"):
        Path(str(db_path) + extra).unlink(missing_ok=True)
    print(f"kept {counts['paper_bets']} paper bets, {counts['decisions']} decisions, "
          f"{counts['markets']} recent markets", flush=True)
    print(f"database now: {_mb(db_path):,.1f} MB. Done.", flush=True)
    return db_path


def _copy(db_path: Path, new_path: Path, lean: bool) -> dict:
    Store(new_path).close()  # create the tables
    conn = sqlite3.connect(str(new_path))
    conn.execute("ATTACH DATABASE ? AS old", (str(db_path),))
    old_tables = {r[0] for r in conn.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
    with conn:
        for table in KEEP_ALL:
            if table not in old_tables:
                continue
            if table not in {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master")}:
                sql = conn.execute(
                    "SELECT sql FROM old.sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                conn.execute(sql)
            if table == "decisions" and lean:
                # Keep every decision, but drop the bulky details of routine
                # price checks older than a week.
                conn.execute(
                    "INSERT INTO main.decisions SELECT id, made_at, kind, ticker, side, action, win_prob, "
                    "price, edge, stake, contracts, dry_run, reason, CASE WHEN action = 'price_check' "
                    "AND replace(substr(made_at, 1, 19), 'T', ' ') < datetime('now', '-7 days') "
                    "THEN NULL ELSE raw END FROM old.decisions"
                )
            else:
                conn.execute(f"INSERT INTO main.{table} SELECT * FROM old.{table}")
        conn.execute(
            "INSERT INTO main.run_log SELECT * FROM old.run_log "
            "WHERE replace(substr(started_at, 1, 19), 'T', ' ') >= datetime('now', ?)",
            (f"-{KEEP_RUN_LOG_DAYS} days",),
        )
        # Markets are a cache that the next scan refills; keep only the ones
        # an open paper bet refers to.
        conn.execute(
            "INSERT INTO main.markets SELECT * FROM old.markets "
            "WHERE ticker IN (SELECT ticker FROM old.paper_bets WHERE status = 'open')"
        )
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
              for t in ("paper_bets", "decisions", "markets")}
    conn.execute("DETACH DATABASE old")
    conn.close()
    return counts


def main() -> None:
    settings = load_settings()
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.db_path
    compact(path)


if __name__ == "__main__":
    main()

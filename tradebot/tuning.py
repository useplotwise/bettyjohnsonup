"""Self-tuning: the bot learns from its own finished pretend bets.

Every cycle it looks at bets that finished in the last WINDOW_DAYS days and
groups them by kind (combo "pick" or single "pregame" bet) and number of legs
(1, 2, 3-4, 5+). Two adjustments follow:

1. Overconfidence. If a group has won less often than the bot said it would,
   its win-chance guesses are scaled down by the same ratio. They are never
   scaled up, and a small sample counts for little (PRIOR), so a handful of
   unlucky bets can't swing it.
2. Losing groups. A group with at least MIN_SETTLED finished bets and a net
   loss is paused: no new bets of that kind until its bad results are older
   than WINDOW_DAYS, when it gets another try.

Paper bets store the bot's raw guess (before scaling), so this always compares
what the model originally said with what happened.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .storage import Store

WINDOW_DAYS = 14
MIN_SETTLED = 30
PRIOR = 30
MIN_FACTOR = 0.25
KINDS = ("pick", "pregame")


def leg_bucket(legs: int) -> str:
    if legs <= 1:
        return "1"
    if legs == 2:
        return "2"
    return "3-4" if legs <= 4 else "5+"


@dataclass
class Group:
    n: int = 0
    won: int = 0
    expected: float = 0.0  # sum of the bot's win chances = expected number of wins
    pnl: float = 0.0

    @property
    def factor(self) -> float:
        if self.n == 0 or self.expected <= 0:
            return 1.0
        ratio = self.won / self.expected
        weight = self.n / (self.n + PRIOR)
        return round(min(1.0, max(MIN_FACTOR, 1 - (1 - ratio) * weight)), 4)

    @property
    def paused(self) -> bool:
        return self.n >= MIN_SETTLED and self.pnl < 0


class Tuner:
    def __init__(self, store: Store, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
        rows = store.conn.execute(
            "SELECT kind, win_prob, status, pnl, COALESCE(json_array_length(raw, '$.legs'), 1) AS legs "
            "FROM paper_bets WHERE kind IN (?, ?) AND status IN ('won', 'lost') "
            "AND win_prob IS NOT NULL AND settled_at >= ?",
            (*KINDS, since),
        ).fetchall()
        self.groups: dict[tuple[str, str], Group] = {}
        for r in rows:
            g = self.groups.setdefault((r["kind"], leg_bucket(r["legs"])), Group())
            g.n += 1
            g.won += r["status"] == "won"
            g.expected += r["win_prob"]
            g.pnl += r["pnl"] or 0.0

    def _group(self, kind: str, legs: int) -> Group:
        return self.groups.get((kind, leg_bucket(legs)), Group())

    def factor(self, kind: str, legs: int) -> float:
        return self._group(kind, legs).factor

    def adjust(self, kind: str, legs: int, prob: float) -> float:
        """The bot's win-chance guess, scaled down if this kind of bet has been overconfident."""
        return prob * self.factor(kind, legs)

    def paused(self, kind: str, legs: int) -> bool:
        return self._group(kind, legs).paused

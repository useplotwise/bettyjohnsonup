"""Write a daily scorecard (logs/scorecard-YYYY-MM-DD.md) from the decisions table."""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .paper import paper_summary
from .storage import Store


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _paper_lines(store: Store) -> list[str]:
    picks, watch, built = (paper_summary(store, k) for k in ("pick", "watch", "built"))
    return [
        "## Paper trading (pretend bets, all time)",
        "",
        f"- Bot's picks: {picks['open']} waiting, {picks['settled']} finished, {picks['won']} won. "
        f"Pretend profit/loss: ${picks['pnl']:+.2f} on ${picks['staked']:.2f} staked.",
        f"- Every quoted combo (1 contract each, picked or not): {watch['open']} waiting, "
        f"{watch['settled']} finished, {watch['won']} won. Pretend profit/loss: ${watch['pnl']:+.2f}.",
        f"- How good are the bot's chances? It expected to win {_pct(picks['expected_win_rate'])} of its "
        f"picks and actually won {_pct(picks['actual_win_rate'])}. On all quoted combos it expected "
        f"{_pct(watch['expected_win_rate'])} and they won {_pct(watch['actual_win_rate'])}.",
        f"- Combos the bot built itself (1 contract each): {built['open']} waiting, "
        f"{built['settled']} finished, {built['won']} won. Pretend profit/loss: ${built['pnl']:+.2f}. "
        f"Expected to win {_pct(built['expected_win_rate'])}, actually won {_pct(built['actual_win_rate'])}.",
        "",
    ] + _built_by_legs(store)


def _built_by_legs(store: Store) -> list[str]:
    rows = store.conn.execute(
        "SELECT raw, status, win_prob, price, pnl FROM paper_bets WHERE kind = 'built'"
    ).fetchall()
    groups: dict[int, list] = {}
    for r in rows:
        legs = len((json.loads(r["raw"]) if r["raw"] else {}).get("legs", []))
        groups.setdefault(legs, []).append(r)
    if not groups:
        return []
    lines = [
        "### Bot-built combos by number of legs",
        "",
        "| Legs | Built | Finished | Won | Avg fair chance | Avg price | Pretend P&L |",
        "|---|---|---|---|---|---|---|",
    ]
    for legs in sorted(groups):
        g = groups[legs]
        done = [r for r in g if r["status"] in ("won", "lost")]
        lines.append(
            f"| {legs} | {len(g)} | {len(done)} | {sum(r['status'] == 'won' for r in done)} | "
            f"{sum(r['win_prob'] or 0 for r in g) / len(g):.1%} | "
            f"${sum(r['price'] for r in g) / len(g):.2f} | ${sum(r['pnl'] or 0 for r in done):+.2f} |"
        )
    return lines + [""]


def write_scorecard(store: Store, log_dir: Path, balance: float | None, dry_run: bool) -> Path:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = store.decisions_since(today)
    cycles = store.conn.execute(
        "SELECT COUNT(*) FROM run_log WHERE started_at >= ?", (today,)
    ).fetchone()[0]
    counts = Counter(r["action"] for r in rows)
    buys = sorted((r for r in rows if r["action"] == "would_buy"), key=lambda r: -(r["edge"] or 0))

    lines = [
        f"# Daily scorecard: {today}",
        "",
        f"- Mode: {'DRY RUN (no real bets placed)' if dry_run else 'LIVE'}",
        f"- Balance: {'unknown' if balance is None else f'${balance:.2f}'}",
        f"- Scan cycles today: {cycles}",
        f"- Bets the bot would place: {counts['would_buy']} "
        f"(total ${sum(r['stake'] or 0 for r in buys):.2f})",
        f"- Combo prices requested from market makers: {counts['price_check']} "
        f"({sum(1 for r in rows if r['action'] == 'price_check' and r['price'])} got a quote)",
        f"- Waiting for your approval (over $500): {counts['needs_approval']}",
        f"- Cash-outs it would make: {counts['would_sell']}",
        "",
    ]
    if buys:
        lines += [
            "## Top picks today",
            "",
            "| Combo | Price | Pays | Fair chance | Edge | Stake |",
            "|---|---|---|---|---|---|",
        ]
        for r in buys[:15]:
            lines.append(
                f"| {r['ticker']} | ${r['price']:.2f} | {1 / r['price']:.0f}x | "
                f"{r['win_prob']:.1%} | {r['edge']:.0%} | ${r['stake']:.2f} |"
            )
        lines.append("")
    lines += _paper_lines(store)
    lines.append(
        "Fair chance multiplies each leg's market price and assumes the legs are "
        "independent. Legs from the same game often aren't, so treat it as an estimate."
    )

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"scorecard-{today}.md"
    path.write_text("\n".join(lines) + "\n")
    return path

"""Write a daily scorecard (logs/scorecard-YYYY-MM-DD.md) from the decisions table."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .storage import Store


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
    lines.append(
        "Fair chance multiplies each leg's market price and assumes the legs are "
        "independent. Legs from the same game often aren't, so treat it as an estimate."
    )

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"scorecard-{today}.md"
    path.write_text("\n".join(lines) + "\n")
    return path

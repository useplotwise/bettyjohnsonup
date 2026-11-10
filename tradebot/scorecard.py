"""Write a daily scorecard (logs/scorecard-YYYY-MM-DD.md) from the decisions table."""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .live import bot_ledger, live_enabled, money_available, real_pnl_today, safety_stop
from .paper import paper_summary
from .storage import Store
from .tuning import KINDS, MIN_SETTLED, WINDOW_DAYS, Tuner


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _paper_lines(store: Store) -> list[str]:
    picks, watch, built, value, old, weather = (paper_summary(store, k)
                                                for k in ("pick", "watch", "built", "pregame", "value", "weather"))
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
        f"- Single bets where Kalshi was cheaper than the sportsbooks (before the game starts): {value['open']} waiting, "
        f"{value['settled']} finished, {value['won']} won. Pretend profit/loss: ${value['pnl']:+.2f} "
        f"on ${value['staked']:.2f} staked. Books said {_pct(value['expected_win_rate'])}, "
        f"actually won {_pct(value['actual_win_rate'])}.",
        f"- Weather bets (NWS forecast vs Kalshi's temperature markets): {weather['open']} waiting, "
        f"{weather['settled']} finished, {weather['won']} won. Pretend profit/loss: ${weather['pnl']:+.2f} "
        f"on ${weather['staked']:.2f} staked. Forecast said {_pct(weather['expected_win_rate'])}, "
        f"actually won {_pct(weather['actual_win_rate'])}.",
        f"- Old single bets (retired: that version also bet games already under way): {old['settled']} finished, "
        f"{old['won']} won, pretend profit/loss ${old['pnl']:+.2f}.",
        "",
    ] + _cashout_lines(store) + _built_by_legs(store)


def _cashout_lines(store: Store) -> list[str]:
    rows = store.conn.execute(
        "SELECT status, price, contracts, fee, pnl, json_extract(raw, '$.cashout.proceeds') AS proceeds "
        "FROM paper_bets WHERE json_extract(raw, '$.cashout.proceeds') IS NOT NULL AND kind != 'live'"
    ).fetchall()
    done = [r for r in rows if r["status"] in ("won", "lost")]
    sold = sum(r["proceeds"] - (r["price"] * r["contracts"] + r["fee"]) for r in done)
    held = sum(r["pnl"] or 0 for r in done)
    return [
        f"- Cash-outs (pretend): {len(rows)} bets flagged for selling early. Of the {len(done)} that have "
        f"finished, selling when flagged would have made ${sold:+.2f}; holding made ${held:+.2f}.",
        "",
    ]


def _tuning_lines(store: Store) -> list[str]:
    tuner = Tuner(store)
    names = {"pick": "Combo picks", "pregame": "Single bets", "weather": "Weather bets"}
    lines = [f"## Self-tuning (bets finished in the last {WINDOW_DAYS} days)", ""]
    found = False
    for kind in KINDS:
        for (k, bucket), g in sorted(tuner.groups.items()):
            if k != kind:
                continue
            found = True
            label = f"{names[k]}, {bucket} leg{'s' if bucket != '1' else ''}"
            note = f"guesses scaled to {g.factor:.0%} of the original"
            if g.paused:
                note += f"; PAUSED (net loss ${-g.pnl:.2f} over {g.n} bets, retried when the losses age out)"
            lines.append(
                f"- {label}: {g.n} finished, expected to win {g.expected / g.n:.0%}, "
                f"actually won {g.won / g.n:.0%}; {note}."
            )
    if not found:
        lines.append("- Nothing finished yet, so nothing is adjusted.")
    lines.append(f"- A group needs {MIN_SETTLED} finished bets and a net loss before it is paused.")
    return lines + [""]


def _built_by_legs(store: Store) -> list[str]:
    rows = store.conn.execute(
        "SELECT raw, status, win_prob, price, pnl FROM paper_bets WHERE kind = 'built'"
    ).fetchall()
    groups: dict[tuple[str, int], list] = {}
    for r in rows:
        raw = json.loads(r["raw"]) if r["raw"] else {}
        style = "long shot" if raw.get("longshot") else "safe"
        groups.setdefault((style, len(raw.get("legs", []))), []).append(r)
    if not groups:
        return []
    lines = [
        "### Bot-built combos by number of legs",
        "",
        "| Style | Legs | Built | Finished | Won | Avg fair chance | Avg price | Pretend P&L |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for style, legs in sorted(groups):
        g = groups[(style, legs)]
        done = [r for r in g if r["status"] in ("won", "lost")]
        lines.append(
            f"| {style} | {legs} | {len(g)} | {len(done)} | {sum(r['status'] == 'won' for r in done)} | "
            f"{sum(r['win_prob'] or 0 for r in g) / len(g):.1%} | "
            f"${sum(r['price'] for r in g) / len(g):.2f} | ${sum(r['pnl'] or 0 for r in done):+.2f} |"
        )
    return lines + [""]


def _mode(dry_run: bool, settings) -> str:
    if dry_run:
        return "DRY RUN (no real bets placed)"
    if settings is not None and not live_enabled(settings):
        return "DRY_RUN is off but BOT_BANKROLL is 0, so no real bets are placed"
    return "LIVE (real bets)"


def _live_lines(store: Store, settings) -> list[str]:
    ledger = bot_ledger(store)
    if settings is None or (not live_enabled(settings) and not ledger["open"] and not ledger["settled"]):
        return []
    return [
        "## Real bets (all time)",
        "",
        f"- Bot money: ${settings.bot_bankroll:.2f} to start, profit so far ${ledger['profit']:+.2f}, "
        f"${ledger['in_open_bets']:.2f} in open bets, ${money_available(settings, store):.2f} free to bet.",
        f"- Bets: {ledger['open']} waiting, {ledger['settled']} finished, {ledger['won']} won. "
        f"Placed today: {ledger['bets_today']} of {settings.max_live_bets_per_day} allowed.",
        f"- Loss stops: today ${real_pnl_today(store):+.2f} (daily limit "
        f"${settings.max_daily_loss_fraction * settings.bot_bankroll:.2f}), total ${ledger['profit']:+.2f} "
        f"(stop at -${settings.max_drawdown_fraction * settings.bot_bankroll:.2f}). "
        f"{safety_stop(settings, store).capitalize() or 'Betting is allowed.'}",
        "",
    ]


def write_scorecard(
    store: Store, log_dir: Path, balance: float | None, dry_run: bool, settings=None
) -> Path:
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
        f"- Mode: {_mode(dry_run, settings)}",
        f"- Balance: {'unknown' if balance is None else f'${balance:.2f}'}",
        f"- Scan cycles today: {cycles}",
        f"- Bets the bot would place: {counts['would_buy']} "
        f"(total ${sum(r['stake'] or 0 for r in buys):.2f})",
        f"- Combo prices requested from market makers: {counts['price_check']} "
        f"({sum(1 for r in rows if r['action'] == 'price_check' and r['price'])} got a quote)",
        f"- Waiting for your approval (over $500): {counts['needs_approval']}",
        f"- Cash-outs it would make: {counts['would_sell']}",
        f"- Real bets placed today: {counts['bought']} (skipped: {counts['live_skipped']})",
        f"- Real cash-outs today: {counts['sold']} (sell orders that didn't fill: {counts['sell_skipped']})",
        "",
    ]
    lines += _live_lines(store, settings)
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
    lines += _tuning_lines(store)
    lines.append(
        "Fair chance multiplies each leg's market price and assumes the legs are "
        "independent. Legs from the same game often aren't, so treat it as an estimate."
    )

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"scorecard-{today}.md"
    path.write_text("\n".join(lines) + "\n")
    return path

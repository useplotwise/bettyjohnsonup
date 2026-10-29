# Trade bot

An automated trading bot for prediction markets (Kalshi first), with multi-leg
sports combos as the top priority and other categories scanned for
positive-expected-value trades.

**Status: Phase 3 (betting logic in dry run). This code places no orders.**

## Plan

1. **Set up and collect data** (this step): Kalshi API client, market and
   order book collectors, sports results collector, SQLite storage, 5-minute loop.
2. **Decision logic in dry run**: fair-value and combo probability models,
   exit ("cash-out") rules, risk caps ($20 default, 85% exception, $500 manual
   approval gate), daily scorecard.
3. **Backtest and paper trade** on Kalshi's demo exchange for 2 to 4 weeks.
4. **Deploy and go live small** on a DigitalOcean droplet with $1 contracts first.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # defaults to the demo exchange with DRY_RUN=true
python -m tradebot.main --once
pytest -q
```

Public market data needs no API key. To also read your balance, create an API
key in Kalshi (Account settings, API keys), save the private key file outside
the repo, and set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env`.
Use a demo-exchange key until step 4.

Data lands in `data/tradebot.sqlite3` (tables `markets`, `orderbook_snapshots`,
`sports_games`, `run_log`) and logs in `logs/tradebot.log`.

## Deploy to DigitalOcean

Create an Ubuntu droplet (the smallest size is enough), then as root:

```bash
curl -O https://raw.githubusercontent.com/useplotwise/bettyjohnsonup/main/deploy/setup_droplet.sh
bash setup_droplet.sh https://github.com/useplotwise/bettyjohnsonup.git
```

If the repo is private, clone it with a deploy key or personal access token
instead. Then follow the printed instructions to add your key and start the
service. Check it with `journalctl -u tradebot -f`.

## Betting logic (dry run)

Each cycle the bot:

1. Prices up to `COMBOS_TO_EVALUATE` combos. A combo's fair chance is the
   product of its legs' market prices (settled legs count as certain). Combos
   have no listed price, so for `RFQ_CHECKS_PER_CYCLE` of them the bot sends a
   request for quote, reads market makers' replies for 10 seconds and withdraws
   the request without accepting (no money moves). Edge is expected profit per
   $1 at the best quote, after Kalshi's fee.
2. Sizes any combo with edge of at least `MIN_EDGE` (see `tradebot/bankroll.py`):
   $2 to $5 to start, growing with lifetime profit up to $20; more only with an
   85%+ win chance and at most half the profit buffer; over $500 needs approval.
3. Checks open positions for cash-outs (selling at the bid) when selling beats
   holding, or locks in 3x the stake while the remaining legs are likely to miss.
4. Paper trades (`tradebot/paper.py`): every would-buy pick becomes a pretend
   bet at its stake, and every quoted combo becomes a 1-contract "watch" bet.
   Up to 50 open pretend bets are checked per cycle and settled against
   Kalshi's real results, after fees.
5. Records every decision in the `decisions` table and rewrites today's
   scorecard at `logs/scorecard-YYYY-MM-DD.md`, including all-time paper
   profit/loss and how often the bot's win chances came true.

Read today's scorecard on the server:

```bash
cat /home/tradebot/bettyjohnsonup/logs/scorecard-$(date -u +%F).md
```

## Notes

- Kalshi combos are "multivariate event" markets. They're detected by their
  `mve_*` fields or `KXMVE` ticker prefix; confirm against live data.
- Sports data comes from ESPN's free public scoreboard for now (game results,
  not player stats). Player-level stats come next.

# Trade bot

An automated trading bot for prediction markets (Kalshi first), with multi-leg
sports combos as the top priority and other categories scanned for
positive-expected-value trades.

**Status: Phase 4 built, off by default. Real bets need DRY_RUN=false AND BOT_BANKROLL above 0.**

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
   The combo builder (`tradebot/strategy/builder.py`) also makes
   `BUILDS_PER_CYCLE` combos of its own: likely legs (65% to 95%) with tight
   prices, one per game, rotating through 2, 3, 4, 6 and 8 legs. It creates
   each combo on Kalshi (free, no order), asks for a quote, and treats it like
   any other combo from there.
   With `ODDS_API_KEY` set (`tradebot/odds.py`), the bot also fetches sharp
   sportsbook odds for game winners, matches them to Kalshi markets by team
   names and date, and uses the books' chance for those legs. The builder
   picks legs Kalshi sells cheaper than the books first, and
   `tradebot/strategy/value.py` paper trades single game-winner markets where
   Kalshi's price is at least `MIN_EDGE` better than the books (kind "value").
4. Paper trades (`tradebot/paper.py`): every would-buy pick becomes a pretend
   bet at its stake, every quoted combo becomes a 1-contract "watch" bet, and
   every quoted combo the bot built becomes a 1-contract "built" bet.
   Up to 50 open pretend bets are checked per cycle and settled against
   Kalshi's real results, after fees.
4b. Same-day thermometer bets (`tradebot/strategy/weather_today.py`, paper kind
   "weather_today"): every 10 minutes the bot reads today's temperatures from
   the exact station each Kalshi temperature market settles on (free NWS feed,
   `tradebot/observations.py`). When the readings have already settled a
   market (for example the high has hit 78F, so "75 or below" is lost) by at
   least 1.5F, and Kalshi's price still leaves `MIN_EDGE`, it records a pretend
   bet at a 99% win chance. One bet per city-day, standard stake cap, its own
   self-tuning group. Pretend bets only.
5. Records every decision in the `decisions` table and rewrites today's
   scorecard at `logs/scorecard-YYYY-MM-DD.md`, including all-time paper
   profit/loss and how often the bot's win chances came true.

Read today's scorecard on the server:

```bash
cat /home/tradebot/bettyjohnsonup/logs/scorecard-$(date -u +%F).md
```

## Turning on real bets

Real betting is off unless both switches are set in `.env` on the server:

```bash
DRY_RUN=false
BOT_BANKROLL=20          # dollars the bot may use; it never touches the rest of your balance
MAX_LIVE_BETS_PER_DAY=5
LIVE_MAX_STAKE=1          # every real bet is at most this; start at $1, raise to 5 after the test
LIVE_SINGLES=false        # only combo picks go live; single game bets stay pretend
```

Then `systemctl restart tradebot`. For each pick, the bot gets a fresh quote for
the stake, accepts it only if it still clears `MIN_EDGE`, waits for the market
maker to confirm, and records the price actually filled. Bets over $500 are
never placed automatically. Cash-outs are still recommendations only (logged as
`would_sell`); the bot does not sell positions yet. To stop real betting, set
`DRY_RUN=true` and restart.

## Notes

- Kalshi combos are "multivariate event" markets. They're detected by their
  `mve_*` fields or `KXMVE` ticker prefix; confirm against live data.
- Sports data comes from ESPN's free public scoreboard for now (game results,
  not player stats). Player-level stats come next.

# signal-trader

Turns investing signals extracted from podcast and finance transcripts (`signals.json`) into **Alpaca paper** trades, compares each one to a hypothetical SPY purchase, and reports the results.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # add your Alpaca PAPER key and secret; .env is gitignored
```

Credentials are read only from environment variables or `.env`, and are never printed. The scripts refuse to run unless `ALPACA_BASE_URL` is `https://paper-api.alpaca.markets`. Before any order, they also check that the account is a paper account (account number starts with `PA`).

## Run

```bash
python ingest.py           # signals.json -> signal_trader.db (episodes, signals, orders, benchmarks)
python trade.py            # DRY RUN (default): planned orders + skipped signals, also in reports/*.csv
python trade.py --paper    # place the entry orders on Alpaca paper
python exits.py            # DRY RUN: positions past their horizon
python exits.py --paper    # sell them
python monitor.py          # sync fills/positions -> reports/report.html, signals.csv, leaderboard.csv
./run_daily.sh [--paper]   # all of the above, only on US trading days
python -m unittest discover -s tests -v
```

## Rules (`rules.yaml`)

- **Eligible:** `ticker_stated` is set and is a real symbol, sentiment is `bullish`, confidence is `medium` or `high`. Every other mention is stored with status `skipped` and the reason (bearish, no stated ticker, low confidence and so on).
- **Excluded episodes:** podcasts starting with "Table Four" dated 2026-11-20. Their metadata needs fixing, so their mentions are logged as skipped. To lift this, remove the entry under `episode_exclusions`.
- **One position per ticker per episode:** when one episode mentions the same ticker more than once, only the mention with the highest confidence is traded.
- **Tickers:** normalized to Alpaca format (`brk-b`, `BRK/B` → `BRK.B`). A ticker is skipped if Alpaca says it isn't tradable or doesn't support dollar-amount orders.
- **Entry:** a $1,000 notional market order (DAY) for the next market open after the episode date. A signal processed after that open is entered at the next session, and the dry run shows how many days late it is.
- **Exit:** sold 20, 60 or 180 trading days after the entry fill (short, medium, long). Free-text horizons are mapped to these buckets by `horizon_keywords`.
- **Benchmark:** a hypothetical $1,000 SPY purchase priced at the entry fill time, and sold at the exit fill time. It is recorded in the `benchmarks` table and never ordered.
- **Idempotent:** entry orders use `client_order_id = episode_id-TICKER`, exits use `…-exit`. Every order is looked up on Alpaca before it is submitted.

## Report

`reports/report.html` is a single page:
- summary tiles
- a leaderboard of hit rate (share of signals with a positive return), average return, average alpha versus SPY, and P&L, by speaker, source and podcast
- a positions table

The same data is in `signals.csv` and `leaderboard.csv`.

## Schedule

`.github/workflows/daily.yml` runs the tests and then `run_daily.sh` at 13:07 UTC on weekdays, before the US open. The script exits early on market holidays. The workflow uploads `reports/` as a build artifact.

1. Add the repository secrets `ALPACA_KEY` and `ALPACA_SECRET`.
2. The workflow stays a dry run until you set the repository variable `LIVE_PAPER=true`.
3. The runner keeps no database. Order state is rebuilt from Alpaca on each run using the client_order_ids, and benchmark prices are re-derived from market data.

Local cron alternative (server clock in ET):

```
7 9 * * 1-5  cd /path/to/signal-trader && ./run_daily.sh --paper >> run.log 2>&1
```

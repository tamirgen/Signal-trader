# signal-trader

Watches finance podcasts, YouTube channels and Fed press conferences for new episodes. Claude extracts the investing views from each transcript into `signals.json`, the qualifying ones become **Alpaca paper** trades, and each trade is compared with a hypothetical SPY purchase. Every day, a dashboard and a "what and why" activity report are published.

## How to follow what's happening

| I want to see… | Go to |
|---|---|
| **My actual investments** (positions, orders, balance; paper money) | https://app.alpaca.markets → switch to **Paper** (top left) → **Positions**, **Orders**, **Portfolio** |
| **Performance vs SPY, leaderboard, recent signals** | Dashboard: https://tamirgen.github.io/signal-trader/ |
| **What happened today and why** | `activity/` folder: one file per day, https://github.com/tamirgen/signal-trader/tree/main/activity |
| **Did the daily job run, did anything fail** | https://github.com/tamirgen/signal-trader/actions |
| **Every signal extracted so far** | `signals.json` |

Nothing here is done by hand. Each morning GitHub runs the code in this repo (no person or AI chat is involved), and the code places the orders in your Alpaca paper account.

## Daily pipeline (`run_daily.sh`, run by GitHub Actions every morning)

1. `fetch.py`: checks each source in `sources.yaml` and the `inbox/` folder for new episodes, gets the transcript, has Claude extract the signals, and appends them to `signals.json`.
2. `ingest.py`: loads `signals.json` into SQLite and applies the rules.
3. On US trading days, `trade.py` places entries and `exits.py` closes positions whose horizon has ended.
4. `activity.py`: writes the daily report (`activity/YYYY-MM-DD.md`, plus the run's summary page on GitHub).
5. `monitor.py`: builds the dashboard (`reports/report.html`), which is published to GitHub Pages.

The job commits new signals, feed state and the activity log back to the repo, so `signals.json` and `activity/` are the full history.

## Sources (`sources.yaml`)

| Type | How new episodes are found | Transcript |
|---|---|---|
| `youtube` | the channel's video feed | YouTube captions |
| `podcast` | the show's RSS feed, found automatically in Apple Podcasts | the feed's own transcript, or Deepgram speech-to-text on the audio |
| `fomc` | federalreserve.gov press conference list | the Fed's transcript PDF |
| `inbox/` | any `.txt` / `.pdf` you upload there (earnings calls, filings, letters) | the file itself |

Only episodes from the last `lookback_days` are considered, at most `max_episodes_per_source` per source per day. Videos with short transcripts (clips) are skipped. Spotify has no public transcript API, so it can't be used as a source.

## Secrets and settings (step-by-step instructions: [SETUP.md](SETUP.md))

| Name | Kind | Needed for |
|---|---|---|
| `ALPACA_KEY`, `ALPACA_SECRET` | secret | trading and the dashboard (paper keys) |
| `ANTHROPIC_API_KEY` | secret | extracting signals from transcripts (Claude) |
| `DEEPGRAM_API_KEY` | secret, optional | podcasts whose feed has no transcript |
| `YOUTUBE_PROXY_URL` | secret, optional | if YouTube blocks GitHub's servers (`http://user:pass@host:port`) |
| `LIVE_PAPER` | variable | `true` = place paper orders; anything else = dry run |

**Dashboard:** enable once in Settings → Pages → Source: **GitHub Actions**. It is then published at `https://<owner>.github.io/<repo>/`. The repo is public, so the dashboard is too: it shows paper positions and P&L, never keys.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # add your Alpaca PAPER key and secret; .env is gitignored
```

Credentials are read only from environment variables or `.env`, and are never printed. The scripts refuse to run unless `ALPACA_BASE_URL` is `https://paper-api.alpaca.markets`. Before any order, they also check that the account is a paper account (account number starts with `PA`).

## Run

```bash
python fetch.py            # new episodes -> transcripts -> Claude -> signals.json (--dry-run: find only)
python ingest.py           # signals.json -> signal_trader.db (episodes, signals, orders, benchmarks)
python trade.py            # DRY RUN (default): planned orders + skipped signals, also in reports/*.csv
python trade.py --paper    # place the entry orders on Alpaca paper
python exits.py            # DRY RUN: positions past their horizon
python exits.py --paper    # sell them
python activity.py         # today's what-and-why report -> activity/YYYY-MM-DD.md
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

`.github/workflows/daily.yml` runs every day at 12:17 UTC (7:17 ET in winter, 8:17 ET in summer):
- It fetches new episodes every day.
- On trading days it also trades before the 9:30 ET open, so orders fill at the open.
- It runs the tests first, uploads `reports/` as a build artifact, and deploys the dashboard.
- The runner keeps no database. Order state is rebuilt from Alpaca using the client_order_ids, and benchmark prices are re-derived from market data.

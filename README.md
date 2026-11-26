# signal-trader

Watches finance podcasts, YouTube channels and Fed press conferences for new episodes. Claude extracts the investing views from each transcript into `signals.json`, the qualifying ones become **Alpaca paper** trades, and each trade is compared with a hypothetical SPY purchase. Every day, a dashboard and a "what and why" activity report are published.

## How to follow what's happening

| I want to see… | Go to |
|---|---|
| **My actual investments** (positions, orders, balance; paper money) | https://app.alpaca.markets → switch to **Paper** (top left) → **Positions**, **Orders**, **Portfolio** |
| **Performance vs SPY, leaderboard, recent signals** | Dashboard: https://tamirgen.github.io/Signal-trader/ |
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

## Position sizing and double-down (`sizing:` in `rules.yaml`)

- **At most $500 in any single stock or ETF.**
- **Exception: with 2 or more independent sources backing the same ticker (3 is better), we double down to $1,000**, which is the ceiling. A source is a distinct show or event (repeat episodes of one show count once), counted within 14 days. When a second source backs a ticker we already hold, the system buys only the difference.
- Every order's size, the sources behind it, and what was already held are written into the daily report.

## Paper account only

This project only ever trades Alpaca's **paper account** (simulated money). It cannot trade a real account:
- The code refuses to start unless `ALPACA_BASE_URL` is exactly `https://paper-api.alpaca.markets`, and the workflow hard-codes that URL.
- Before any order it checks that the account number starts with `PA` (Alpaca's paper accounts) and aborts otherwise.
- Real-account keys are rejected by Alpaca's paper server anyway. Tests in `tests/test_safety.py` cover both checks.

## Test plan

The system runs unattended for about one month (from 2026-11-27) as a test of the tool: how its stock calls, theme ideas and double-down rule perform against SPY. Holding periods are 20 / 60 / 180 trading days, so most positions will still be open at the end; results are measured against SPY from each entry (unrealized), not as closed trades.

## Theme logic: from world events to stocks

Besides trading stocks a speaker names, the system turns world events (war, oil, rates, AI build-out, supply chains) into stock ideas.

1. `themes.py` has Claude read each episode's summary, themes and views. For every market-relevant event it picks the matching theme from `universe.yaml` and the ETFs/stocks in it that should benefit, with a cause-and-effect chain and what would prove it wrong. Claude can only choose from the lists in `universe.yaml`; anything else is discarded.
2. `ingest.py` turns the tailwind picks of medium or high strength into a separate **theme book**, measured apart from the stock-pick book. `trade.py` limits it: at most 2 positions per theme, at most $3,000 open in total, no ideas from episodes older than 7 days. Orders are placed automatically (`thematic.mode: paper`) and each is recorded in the daily report. Set `mode: shadow` to stop placing them.
3. **Shadow account** (`shadow.py`): every theme idea, of any strength and age and regardless of the limits, is tracked as a hypothetical $500 purchase (entry at the next open after the episode, exit at its horizon) against SPY. The dashboard shows its hit rate and alpha overall, by idea strength, and for ideas that were vs were not actually traded, so you can see whether the idea logic and the limits add value. Prices come from Alpaca's daily bars (free IEX feed).

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

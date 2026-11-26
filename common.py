"""Shared helpers: config, SQLite, signal filter, market calendar, Alpaca access.

    python common.py --is-trading-day   # exit 0 if today (ET) is a US trading day, 3 if not; anything else is an error
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
PAPER_URL = "https://paper-api.alpaca.markets"
REPORTS = ROOT / "reports"


# ---------- config ----------
def load_rules(path: Path = ROOT / "rules.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_env() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")


# ---------- database ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, podcast TEXT, source TEXT, date TEXT, summary TEXT, title TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    mention_id TEXT PRIMARY KEY,           -- episode_id#index in signals.json
    episode_id TEXT REFERENCES episodes(id),
    asset TEXT, sentiment TEXT, speaker TEXT, confidence TEXT, horizon TEXT, claim_type TEXT,
    ticker_stated TEXT, ticker_inferred TEXT, catalyst_date TEXT, reasoning TEXT, quote TEXT,
    ticker TEXT,                           -- normalized ticker_stated
    horizon_bucket TEXT, hold_days INTEGER,
    status TEXT NOT NULL,                  -- eligible | skipped
    skip_reason TEXT,
    book TEXT NOT NULL DEFAULT 'speaker',  -- 'speaker' (a named stock) | 'theme' (from a world event)
    theme TEXT                             -- theme key from universe.yaml, for the theme book
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    mention_id TEXT REFERENCES signals(mention_id),
    role TEXT,                             -- entry | exit
    symbol TEXT, side TEXT, notional REAL, qty REAL,
    alpaca_order_id TEXT, status TEXT,
    filled_qty REAL, filled_avg_price REAL, filled_at TEXT, submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks (    -- hypothetical SPY buy alongside each entry
    mention_id TEXT PRIMARY KEY REFERENCES signals(mention_id),
    symbol TEXT, notional REAL,
    entry_at TEXT, entry_price REAL, qty REAL,
    exit_at TEXT, exit_price REAL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("SIGNAL_DB", ROOT / "signal_trader.db"))


MIGRATIONS = [  # columns added after the first release; ignored if already present
    "ALTER TABLE episodes ADD COLUMN title TEXT", "ALTER TABLE episodes ADD COLUMN url TEXT",
    "ALTER TABLE signals ADD COLUMN reasoning TEXT", "ALTER TABLE signals ADD COLUMN quote TEXT",
    "ALTER TABLE signals ADD COLUMN book TEXT NOT NULL DEFAULT 'speaker'", "ALTER TABLE signals ADD COLUMN theme TEXT",
    "DROP INDEX IF EXISTS uq_signal_episode_ticker",
    # one tradable signal per ticker per episode, per book
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_book ON signals(episode_id, ticker, book) WHERE status = 'eligible'",
]


def db() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            con.execute(m)
        except sqlite3.OperationalError:
            pass
    return con


# ---------- signal filter ----------
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def normalize_ticker(raw: str | None, aliases: dict | None = None) -> str | None:
    """'brk-b', 'BRK/B', '$BRK.B' -> 'BRK.B'. None if it isn't a single symbol ('Nvidia', 'AMD, MU')."""
    t = (raw or "").strip().upper().replace("$", "")
    t = re.sub(r"[-/ ]", ".", t)
    t = (aliases or {}).get(t, t)
    return t if TICKER_RE.match(t) else None


def episode_exclusion(ep: dict, rules: dict) -> str | None:
    for ex in rules.get("episode_exclusions") or []:
        if (ep.get("podcast") or "").startswith(ex["podcast_prefix"]) and ep.get("date") == ex["date"]:
            return ex.get("reason") or "episode excluded"
    return None


def evaluate_mention(m: dict, rules: dict) -> tuple[str | None, str | None]:
    """Return (normalized ticker or None, skip_reason). skip_reason None means eligible."""
    el = rules["eligibility"]
    stated = (m.get("ticker_stated") or "").strip()
    ticker = normalize_ticker(stated, rules.get("ticker_aliases"))
    sentiment = (m.get("sentiment") or "").lower()
    if el["require_ticker_stated"] and not stated:
        return None, "no stated ticker"
    if stated and not ticker:
        return None, f"stated ticker is not a symbol: {stated!r}"
    if sentiment not in el["sentiments"]:
        return ticker, f"sentiment {sentiment or 'missing'}"
    if (m.get("confidence") or "").lower() not in el["confidences"]:
        return ticker, f"confidence {m.get('confidence') or 'missing'}"
    return ticker, None


def horizon_bucket(horizon: str | None, rules: dict) -> str:
    h = (horizon or "").strip().lower()
    if h in rules["horizons"]:
        return h
    for bucket, words in rules["horizon_keywords"].items():
        if any(w in h for w in words):
            return bucket
    return rules["horizon_default"]


# ---------- market calendar ----------
# NYSE full-day closures; used until Alpaca's calendar is loaded (dry runs without keys, tests).
NYSE_HOLIDAYS = {dt.date.fromisoformat(d) for d in [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04",
    "2028-09-04", "2028-11-23", "2028-12-25",
]}
_alpaca_days: set[dt.date] | None = None


def use_alpaca_calendar(tc) -> None:
    """Replace the built-in holiday list with Alpaca's calendar."""
    global _alpaca_days
    from alpaca.trading.requests import GetCalendarRequest
    start = dt.date.today() - dt.timedelta(days=400)
    cal = tc.get_calendar(GetCalendarRequest(start=start, end=start + dt.timedelta(days=1200)))
    _alpaca_days = {c.date for c in cal}


def is_trading_day(d: dt.date) -> bool:
    if _alpaca_days is not None:
        return d in _alpaca_days
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def next_trading_day(d: dt.date) -> dt.date:
    """First trading day strictly after d: the 'next market open after' a date."""
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def add_trading_days(d: dt.date, n: int) -> dt.date:
    for _ in range(n):
        d = next_trading_day(d)
    return d


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def current_session(now: dt.datetime | None = None) -> dt.date:
    """The trading session an order sent at `now` executes in (today until the 16:00 ET close)."""
    now = (now or now_et()).astimezone(ET)
    if is_trading_day(now.date()) and now.time() < dt.time(16, 0):
        return now.date()
    return next_trading_day(now.date())


# ---------- Alpaca ----------
def _env(name: str) -> str:
    """Env var with surrounding whitespace removed (pasted secrets often carry a space or newline)."""
    return (os.environ.get(name) or "").strip()


def have_keys() -> bool:
    return bool(_env("ALPACA_KEY") and _env("ALPACA_SECRET"))


def trading_client():
    from alpaca.trading.client import TradingClient
    base = (_env("ALPACA_BASE_URL") or PAPER_URL).rstrip("/")
    if base != PAPER_URL:
        raise SystemExit(f"Refusing to run: ALPACA_BASE_URL must be {PAPER_URL}.")
    return TradingClient(_env("ALPACA_KEY"), _env("ALPACA_SECRET"), paper=True)


def assert_paper(tc) -> None:
    """Paper accounts have account numbers starting with 'PA'. Refuse anything else."""
    acct = tc.get_account()
    if not str(acct.account_number).startswith("PA"):
        raise SystemExit("Refusing to trade: the Alpaca account is not a paper account.")


def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(_env("ALPACA_KEY"), _env("ALPACA_SECRET"))


def _coid_base(s) -> str:
    book = s["book"] if "book" in s.keys() else "speaker"
    return f"{s['episode_id']}-{'T-' if book == 'theme' else ''}{s['ticker']}"


def entry_coid(s) -> str:
    return _coid_base(s)[:128]


def exit_coid(s) -> str:
    return (_coid_base(s) + "-exit")[:128]


def fetch_order(tc, coid: str):
    """The order with this client_order_id, or None if Alpaca has never seen it."""
    from alpaca.common.exceptions import APIError
    try:
        return tc.get_order_by_client_id(coid)
    except APIError as e:
        if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
            return None
        raise


def save_order(con, o, mention_id: str, role: str) -> None:
    f = lambda x: float(x) if x is not None else None  # noqa: E731
    con.execute(
        """INSERT INTO orders (client_order_id, mention_id, role, symbol, side, notional, qty,
               alpaca_order_id, status, filled_qty, filled_avg_price, filled_at, submitted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(client_order_id) DO UPDATE SET
               alpaca_order_id=excluded.alpaca_order_id, status=excluded.status,
               filled_qty=excluded.filled_qty, filled_avg_price=excluded.filled_avg_price,
               filled_at=excluded.filled_at""",
        (o.client_order_id, mention_id, role, o.symbol, o.side.value, f(o.notional), f(o.qty),
         str(o.id), o.status.value, f(o.filled_qty), f(o.filled_avg_price),
         o.filled_at.isoformat() if o.filled_at else None,
         o.submitted_at.isoformat() if o.submitted_at else None),
    )


def sync_orders(con, tc) -> None:
    """Refresh every expected order from Alpaca. Alpaca is the source of truth, so a fresh
    database (e.g. on a CI runner) rebuilds its order state from the client_order_ids."""
    for s in con.execute("SELECT * FROM signals WHERE status = 'eligible'").fetchall():
        for role, coid in (("entry", entry_coid(s)), ("exit", exit_coid(s))):
            o = fetch_order(tc, coid)
            if o is not None:
                save_order(con, o, s["mention_id"], role)
    con.commit()


def price_at(symbol: str, ts: dt.datetime, feed: str) -> float | None:
    """Price of `symbol` at time ts: open of the 1-minute bar containing ts (falls back to the day's open)."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc, fd = data_client(), DataFeed(feed)
    minute = ts.replace(second=0, microsecond=0)
    bars = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                                              start=minute, end=minute + dt.timedelta(minutes=1), feed=fd))
    rows = bars.data.get(symbol) or []
    if rows:
        return float(rows[0].open)
    day = ts.astimezone(ET).date()
    bars = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                              start=dt.datetime.combine(day, dt.time(), ET),
                                              end=dt.datetime.combine(day, dt.time(23, 59), ET), feed=fd))
    rows = bars.data.get(symbol) or []
    return float(rows[0].open) if rows else None


def latest_prices(symbols, feed: str) -> dict[str, float]:
    symbols = sorted(set(symbols))
    if not symbols:
        return {}
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest
    trades = data_client().get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed(feed)))
    return {k: float(v.price) for k, v in trades.items()}


def record_benchmarks(con, rules: dict) -> None:
    """For every filled entry, record a hypothetical SPY buy of the SAME DOLLARS at the entry fill time,
    and the matching SPY sale at the exit fill time once the position is closed."""
    b = rules["benchmark"]
    rows = con.execute(
        """SELECT e.mention_id, e.filled_at AS entry_at, x.filled_at AS exit_at, bm.entry_price, bm.exit_price,
                  COALESCE(e.filled_qty * e.filled_avg_price, e.notional) AS dollars
           FROM orders e LEFT JOIN orders x ON x.mention_id = e.mention_id AND x.role = 'exit' AND x.filled_at IS NOT NULL
           LEFT JOIN benchmarks bm ON bm.mention_id = e.mention_id
           WHERE e.role = 'entry' AND e.filled_at IS NOT NULL""").fetchall()
    for r in rows:
        if r["entry_price"] is None:
            px = price_at(b["symbol"], dt.datetime.fromisoformat(r["entry_at"]), rules["data_feed"])
            if px and r["dollars"]:  # the same dollars we put into the stock, not a fixed amount
                con.execute("""INSERT OR REPLACE INTO benchmarks (mention_id, symbol, notional, entry_at, entry_price, qty)
                               VALUES (?,?,?,?,?,?)""",
                            (r["mention_id"], b["symbol"], r["dollars"], r["entry_at"], px, r["dollars"] / px))
        if r["exit_at"] and r["exit_price"] is None:
            px = price_at(b["symbol"], dt.datetime.fromisoformat(r["exit_at"]), rules["data_feed"])
            if px:
                con.execute("UPDATE benchmarks SET exit_at = ?, exit_price = ? WHERE mention_id = ?",
                            (r["exit_at"], px, r["mention_id"]))
    con.commit()


def connect_alpaca(con=None, require: bool = False):
    """Trading client with Alpaca's calendar loaded and orders synced, or None without keys."""
    load_env()
    if not have_keys():
        if require:
            raise SystemExit("--paper needs ALPACA_KEY and ALPACA_SECRET (environment or .env).")
        return None
    tc = trading_client()
    use_alpaca_calendar(tc)
    if con is not None:
        sync_orders(con, tc)
    return tc


if __name__ == "__main__":
    if sys.argv[1:] == ["--is-trading-day"]:
        connect_alpaca()
        today = now_et().date()
        ok = is_trading_day(today)
        print(f"{today} is {'a' if ok else 'not a'} US trading day")
        sys.exit(0 if ok else 3)
    print(__doc__)

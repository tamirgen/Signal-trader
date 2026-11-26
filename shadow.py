"""Shadow account: a hypothetical purchase of EVERY theme idea, whether or not the real limits allowed it.

Each pick is "bought" at the open of the first trading day after its episode and "sold" at the open of its
horizon day (20 / 60 / 180 trading days later), or marked at the latest close while still open. SPY is measured
over the same dates. This measures the idea logic itself, and shows whether the real limits (strength filter,
age, caps) help or hurt. Prices come from Alpaca's daily bars; with the free IEX feed they are IEX-venue prices,
which can differ slightly from the consolidated tape.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from common import ET, add_trading_days, next_trading_day

Bars = dict  # symbol -> {iso date: (open, close)}


def shadow_picks(con) -> list[dict]:
    """Every theme-book pick, any strength, any age."""
    return [dict(r) for r in con.execute(
        """SELECT s.mention_id, s.ticker, s.theme, s.confidence AS strength, s.hold_days, s.asset AS event,
                  e.date AS ep_date, e.podcast,
                  EXISTS (SELECT 1 FROM orders o WHERE o.mention_id = s.mention_id AND o.role = 'entry') AS traded
           FROM signals s JOIN episodes e ON e.id = s.episode_id
           WHERE s.book = 'theme' ORDER BY e.date, s.ticker""")]


def fetch_bars(symbols, start: dt.date, end: dt.date, feed: str) -> Bars:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    out: Bars = {}
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), 50):
        req = StockBarsRequest(symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Day,
                               start=dt.datetime.combine(start, dt.time(), ET),
                               end=dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), ET),
                               feed=DataFeed(feed))
        for sym, bars in client_bars(req).items():
            out[sym] = {b.timestamp.astimezone(ET).date().isoformat(): (float(b.open), float(b.close)) for b in bars}
    return out


def client_bars(req) -> dict:
    from common import data_client
    return data_client().get_stock_bars(req).data


def _price(bars: Bars, sym: str, day: dt.date):
    return (bars.get(sym) or {}).get(day.isoformat())


def _last(bars: Bars, sym: str, today: dt.date):
    series = {d: v for d, v in (bars.get(sym) or {}).items() if d <= today.isoformat()}
    return series[max(series)][1] if series else None


def evaluate(picks: list[dict], bars: Bars, today: dt.date, spy: str = "SPY", notional: float = 500.0) -> list[dict]:
    rows = []
    for p in picks:
        entry = next_trading_day(dt.date.fromisoformat(p["ep_date"]))
        exit_ = add_trading_days(entry, p["hold_days"])
        r = {**p, "entry_date": entry.isoformat(), "exit_date": exit_.isoformat(), "traded": bool(p["traded"])}
        e, b = _price(bars, p["ticker"], entry), _price(bars, spy, entry)
        if entry > today:
            r["status"] = "pending"
        elif not e or not b:
            r["status"] = "no data"
        else:
            closed = exit_ <= today
            x = _price(bars, p["ticker"], exit_) if closed else None
            bx = _price(bars, spy, exit_) if closed else None
            if closed and x and bx:
                mark, spy_mark, r["status"] = x[0], bx[0], "closed"
            else:
                mark, spy_mark, r["status"] = _last(bars, p["ticker"], today), _last(bars, spy, today), "open"
            if mark and spy_mark:
                r.update(entry_px=e[0], mark_px=mark, ret=mark / e[0] - 1, spy_ret=spy_mark / b[0] - 1)
                r["alpha"] = r["ret"] - r["spy_ret"]
                r["pnl"] = notional * r["ret"]
        rows.append(r)
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    """Hit rate and alpha overall, by idea strength, and for ideas the real account did vs did not trade."""
    groups = defaultdict(list)
    for r in rows:
        if "ret" not in r:
            continue
        groups["All ideas"].append(r)
        groups[f"Strength: {r['strength']}"].append(r)
        groups["Traded for real" if r["traded"] else "Not traded for real"].append(r)
    order = ["All ideas", "Strength: high", "Strength: medium", "Strength: low", "Traded for real", "Not traded for real"]
    out = []
    for name in order:
        rs = groups.get(name)
        if rs:
            out.append({"group": name, "ideas": len(rs), "hit_rate": sum(r["ret"] > 0 for r in rs) / len(rs),
                        "avg_return": sum(r["ret"] for r in rs) / len(rs),
                        "avg_alpha": sum(r["alpha"] for r in rs) / len(rs),
                        "beat_spy": sum(r["alpha"] > 0 for r in rs) / len(rs),
                        "pnl": sum(r["pnl"] for r in rs)})
    return out

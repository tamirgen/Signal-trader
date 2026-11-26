"""Place entry orders for eligible signals.

    python trade.py            # dry run (default): print planned orders and skipped signals
    python trade.py --paper    # place the orders on the Alpaca PAPER account

Idempotent: client_order_id = episode_id-TICKER, looked up on Alpaca before every submit,
so re-running never buys the same signal twice.
"""
from __future__ import annotations

import argparse
import csv

from common import (REPORTS, add_trading_days, assert_paper, connect_alpaca, current_session, db,
                    entry_coid, fetch_order, load_rules, next_trading_day, save_order)
import datetime as dt

DEAD = {"canceled", "expired", "rejected"}


def tradable_reason(tc, symbol: str, cache: dict) -> str | None:
    """None if Alpaca can trade a $ notional order in symbol, else why not. Unchecked without keys."""
    if tc is None:
        return None
    if symbol not in cache:
        from alpaca.common.exceptions import APIError
        try:
            a = tc.get_asset(symbol)
            cache[symbol] = (None if a.tradable and a.fractionable else
                             "not tradable on Alpaca" if not a.tradable else "not fractionable (no $ orders)")
        except APIError:
            cache[symbol] = "unknown symbol on Alpaca"
    return cache[symbol]


def open_theme_positions(con) -> list[tuple[str, str]]:
    """(ticker, theme) of theme-book entries that are on Alpaca and not yet exited."""
    return [(r["ticker"], r["theme"]) for r in con.execute(
        """SELECT s.ticker, s.theme FROM orders o JOIN signals s ON s.mention_id = o.mention_id
           WHERE s.book = 'theme' AND o.role = 'entry' AND o.status NOT IN ('canceled', 'expired', 'rejected')
             AND NOT EXISTS (SELECT 1 FROM orders x WHERE x.mention_id = o.mention_id AND x.role = 'exit')""")]


def theme_block_reason(th: dict, held: list[tuple[str, str]], ticker: str, theme: str) -> str | None:
    """Why a theme-book buy must not go ahead, given what is already held or planned."""
    if any(t == ticker for t, _ in held):
        return f"already holding {ticker} from an earlier idea"
    if sum(1 for _, k in held if k == theme) >= th["max_positions_per_theme"]:
        return f"theme limit reached ({th['max_positions_per_theme']} positions in {theme})"
    if (len(held) + 1) * th["notional_usd"] > th["max_total_usd"]:
        return f"total theme limit reached (${th['max_total_usd']:,.0f})"
    return None


def plan_entries(con, rules, tc) -> list[dict]:
    session = current_session()
    th = rules.get("thematic") or {}
    cache: dict = {}
    plan = []
    held = open_theme_positions(con)       # grows as theme buys are planned in this run
    for s in con.execute("""SELECT s.*, e.date AS ep_date, e.podcast, e.source FROM signals s
                            JOIN episodes e ON e.id = s.episode_id
                            WHERE s.status = 'eligible'
                            ORDER BY e.date, s.book,
                                     CASE s.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, s.ticker""").fetchall():
        is_theme = s["book"] == "theme"
        ep_date = dt.date.fromisoformat(s["ep_date"])
        intended = next_trading_day(ep_date)
        notional = float(th["notional_usd"] if is_theme else rules["entry"]["notional_usd"])
        coid = entry_coid(s)
        row = {"client_order_id": coid, "symbol": s["ticker"], "side": "buy", "notional": notional,
               "book": s["book"], "theme": s["theme"],
               "episode": s["episode_id"], "podcast": s["podcast"], "speaker": s["speaker"],
               "confidence": s["confidence"], "horizon": s["horizon"], "bucket": s["horizon_bucket"],
               "hold_days": s["hold_days"], "intended_open": intended.isoformat(),
               "session": session.isoformat(),
               "est_exit": add_trading_days(session, s["hold_days"]).isoformat(),
               "mention_id": s["mention_id"], "reasoning": s["reasoning"], "quote": s["quote"]}
        existing = con.execute("SELECT status FROM orders WHERE client_order_id = ?", (coid,)).fetchone()
        if existing:
            row.update(action="EXISTS", note=f"already on Alpaca ({existing['status']})"
                       + ("; not retried" if existing["status"] in DEAD else ""))
        elif is_theme and (session - ep_date).days > th["max_age_days"]:
            row.update(action="SKIP", note=f"stale idea: episode is {(session - ep_date).days} days old")
        elif is_theme and (why := theme_block_reason(th, held, s["ticker"], s["theme"])):
            row.update(action="SKIP", note=why)
        elif bad := tradable_reason(tc, s["ticker"], cache):
            con.execute("UPDATE signals SET status = 'skipped', skip_reason = ? WHERE mention_id = ?",
                        (bad, s["mention_id"]))
            row.update(action="SKIP", note=bad)
        else:
            late = (session - intended).days
            row.update(action="SHADOW" if is_theme and th.get("mode", "shadow") != "paper" else "BUY",
                       note=(f"{late}d after intended open" if late > 0 else "")
                       + ("" if tc else ("; " if late > 0 else "") + "tradability unchecked (no keys)"))
            if row["action"] == "SHADOW":
                row["note"] = ("theme shadow mode: not traded" + ("; " + row["note"] if row["note"] else ""))
            if is_theme:
                held.append((s["ticker"], s["theme"]))
        plan.append(row)
    con.commit()
    return plan


def place(con, tc, plan) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    for r in plan:
        if r["action"] != "BUY":
            continue
        o = fetch_order(tc, r["client_order_id"])  # last-second idempotency check
        if o is None:
            o = tc.submit_order(MarketOrderRequest(symbol=r["symbol"], notional=r["notional"],
                                                   side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                                   client_order_id=r["client_order_id"]))
        save_order(con, o, r["mention_id"], "entry")
        r["note"] = f"submitted, status {o.status.value}"
    con.commit()


def write_csv(path, rows, cols):
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="print planned orders only (default)")
    g.add_argument("--paper", action="store_true", help="place orders on the Alpaca paper account")
    paper = ap.parse_args().paper

    rules, con = load_rules(), db()
    tc = connect_alpaca(con, require=paper)
    if paper:
        assert_paper(tc)
    plan = plan_entries(con, rules, tc)
    if paper:
        place(con, tc, plan)

    cols = ["action", "book", "theme", "symbol", "side", "notional", "session", "intended_open", "est_exit", "hold_days",
            "bucket", "horizon", "episode", "podcast", "speaker", "confidence", "client_order_id", "note",
            "reasoning", "quote"]
    write_csv(REPORTS / "planned_orders.csv", plan, cols)
    skipped = [dict(r) for r in con.execute(
        """SELECT e.id AS episode, e.podcast, e.date, s.speaker, s.asset, s.ticker_stated, s.sentiment,
                  s.confidence, s.skip_reason FROM signals s JOIN episodes e ON e.id = s.episode_id
           WHERE s.status = 'skipped' ORDER BY s.skip_reason, e.date""")]
    write_csv(REPORTS / "skipped_signals.csv", skipped, list(skipped[0]) if skipped else ["skip_reason"])

    print(("PAPER — orders sent" if paper else "DRY RUN — nothing sent") + f"\n\nPlanned orders ({len(plan)}):")
    for r in plan:
        print(f"  {r['action']:<6} {r['symbol']:<6} ${r['notional']:,.0f}  session {r['session']}  "
              f"(intended {r['intended_open']})  hold {r['hold_days']}d ({r['bucket']}) -> exit ~{r['est_exit']}  "
              f"{r['client_order_id']}  {r['note']}")
    print(f"\nSkipped signals ({len(skipped)}):")
    for r in skipped:
        print(f"  {r['skip_reason']:<55} {r['ticker_stated'] or '-':<8} {r['sentiment']:<8} "
              f"{r['confidence']:<7} {r['episode']}  {r['asset']}")
    print(f"\nCSV: {REPORTS / 'planned_orders.csv'}, {REPORTS / 'skipped_signals.csv'}")


if __name__ == "__main__":
    main()

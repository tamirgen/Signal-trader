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


def plan_entries(con, rules, tc) -> list[dict]:
    session = current_session()
    notional = float(rules["entry"]["notional_usd"])
    cache: dict = {}
    plan = []
    for s in con.execute("""SELECT s.*, e.date AS ep_date, e.podcast, e.source FROM signals s
                            JOIN episodes e ON e.id = s.episode_id
                            WHERE s.status = 'eligible' ORDER BY e.date, s.ticker""").fetchall():
        intended = next_trading_day(dt.date.fromisoformat(s["ep_date"]))
        coid = entry_coid(s)
        row = {"client_order_id": coid, "symbol": s["ticker"], "side": "buy", "notional": notional,
               "episode": s["episode_id"], "podcast": s["podcast"], "speaker": s["speaker"],
               "confidence": s["confidence"], "horizon": s["horizon"], "bucket": s["horizon_bucket"],
               "hold_days": s["hold_days"], "intended_open": intended.isoformat(),
               "session": session.isoformat(),
               "est_exit": add_trading_days(session, s["hold_days"]).isoformat(),
               "mention_id": s["mention_id"]}
        existing = con.execute("SELECT status FROM orders WHERE client_order_id = ?", (coid,)).fetchone()
        if existing:
            row.update(action="EXISTS", note=f"already on Alpaca ({existing['status']})"
                       + ("; not retried" if existing["status"] in DEAD else ""))
        elif bad := tradable_reason(tc, s["ticker"], cache):
            con.execute("UPDATE signals SET status = 'skipped', skip_reason = ? WHERE mention_id = ?",
                        (bad, s["mention_id"]))
            row.update(action="SKIP", note=bad)
        else:
            late = (session - intended).days
            row.update(action="BUY", note=(f"{late}d after intended open" if late > 0 else "")
                       + ("" if tc else ("; " if late > 0 else "") + "tradability unchecked (no keys)"))
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

    cols = ["action", "symbol", "side", "notional", "session", "intended_open", "est_exit", "hold_days",
            "bucket", "horizon", "episode", "podcast", "speaker", "confidence", "client_order_id", "note"]
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

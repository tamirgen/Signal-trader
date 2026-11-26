"""Close positions that have reached their horizon (20 / 60 / 180 trading days after the entry fill).

    python exits.py            # dry run (default): list what is due and what is still held
    python exits.py --paper    # submit the sell orders on the Alpaca PAPER account

Idempotent: client_order_id = episode_id-TICKER-exit.
"""
from __future__ import annotations

import argparse
import datetime as dt

from common import (ET, add_trading_days, assert_paper, connect_alpaca, current_session, db,
                    exit_coid, fetch_order, save_order)


def due_exits(con, session: dt.date) -> list[dict]:
    rows = con.execute(
        """SELECT s.*, o.symbol, o.filled_qty, o.filled_at FROM signals s
           JOIN orders o ON o.mention_id = s.mention_id AND o.role = 'entry' AND o.filled_at IS NOT NULL
           WHERE NOT EXISTS (SELECT 1 FROM orders x WHERE x.mention_id = s.mention_id AND x.role = 'exit')
           ORDER BY o.filled_at""").fetchall()
    out = []
    for r in rows:
        filled = dt.datetime.fromisoformat(r["filled_at"]).astimezone(ET).date()
        exit_on = add_trading_days(filled, r["hold_days"])
        out.append({"mention_id": r["mention_id"], "symbol": r["symbol"], "qty": float(r["filled_qty"]),
                    "entry_date": filled.isoformat(), "exit_on": exit_on.isoformat(),
                    "due": session >= exit_on, "client_order_id": exit_coid(r)})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="list due exits only (default)")
    g.add_argument("--paper", action="store_true", help="submit sells on the Alpaca paper account")
    paper = ap.parse_args().paper

    con = db()
    tc = connect_alpaca(con, require=paper)
    if paper:
        assert_paper(tc)
    exits = due_exits(con, current_session())
    if not exits:
        print("No filled entries awaiting exit.")
    for e in exits:
        if not e["due"]:
            print(f"  HOLD {e['symbol']:<6} qty {e['qty']:.6f}  entered {e['entry_date']}  exit due {e['exit_on']}")
            continue
        qty = round(e["qty"], 9)
        if tc is not None:
            try:  # never sell more than is actually held
                qty = min(qty, float(tc.get_open_position(e["symbol"]).qty))
            except Exception:
                print(f"  SKIP {e['symbol']:<6} no open position on Alpaca")
                continue
        print(f"  SELL {e['symbol']:<6} qty {qty:.6f}  entered {e['entry_date']}  due {e['exit_on']}"
              + ("" if paper else "  (dry run)"))
        if paper:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
            o = fetch_order(tc, e["client_order_id"]) or tc.submit_order(MarketOrderRequest(
                symbol=e["symbol"], qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                client_order_id=e["client_order_id"]))
            save_order(con, o, e["mention_id"], "exit")
    con.commit()


if __name__ == "__main__":
    main()

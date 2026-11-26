"""Pull positions and fills from Alpaca, compute P&L and alpha vs the SPY benchmark,
and write reports/report.html plus reports/signals.csv and reports/leaderboard.csv.

    python monitor.py
"""
from __future__ import annotations

import csv
import datetime as dt
import html
from collections import defaultdict

from common import REPORTS, connect_alpaca, db, latest_prices, load_rules, record_benchmarks


def build_rows(con, marks: dict[str, float]) -> list[dict]:
    rows = []
    for s in con.execute(
            """SELECT s.*, e.podcast, e.source, e.date AS ep_date,
                      en.filled_avg_price AS entry_px, en.filled_qty AS qty, en.filled_at AS entry_at, en.status AS entry_status,
                      ex.filled_avg_price AS exit_px, ex.filled_at AS exit_at, ex.status AS exit_status,
                      b.entry_price AS spy_entry, b.exit_price AS spy_exit
               FROM signals s JOIN episodes e ON e.id = s.episode_id
               LEFT JOIN orders en ON en.mention_id = s.mention_id AND en.role = 'entry'
               LEFT JOIN orders ex ON ex.mention_id = s.mention_id AND ex.role = 'exit'
               LEFT JOIN benchmarks b ON b.mention_id = s.mention_id
               WHERE s.status = 'eligible' OR en.client_order_id IS NOT NULL
               ORDER BY e.date, s.ticker"""):
        r = {"episode": s["episode_id"], "date": s["ep_date"], "podcast": s["podcast"], "source": s["source"],
             "speaker": s["speaker"], "ticker": s["ticker"], "confidence": s["confidence"],
             "hold_days": s["hold_days"], "status": "not placed"}
        if s["entry_px"]:
            closed = s["exit_px"] is not None
            mark = s["exit_px"] if closed else marks.get(s["ticker"])
            r.update(status="closed" if closed else "open", entry_date=s["entry_at"][:10],
                     entry_px=s["entry_px"], qty=s["qty"], mark_px=mark,
                     exit_date=s["exit_at"][:10] if closed else None)
            if mark:
                r["ret"] = mark / s["entry_px"] - 1
                r["pnl"] = s["qty"] * (mark - s["entry_px"])
            spy_mark = s["spy_exit"] if closed else marks.get("SPY")
            if s["spy_entry"] and spy_mark:
                r["spy_ret"] = spy_mark / s["spy_entry"] - 1
                if "ret" in r:
                    r["alpha"] = r["ret"] - r["spy_ret"]
        elif s["entry_status"]:
            r["status"] = f"entry {s['entry_status']}"
        rows.append(r)
    return rows


def leaderboard(rows) -> list[dict]:
    out = []
    for key in ("speaker", "source", "podcast"):
        groups = defaultdict(list)
        for r in rows:
            if "ret" in r:
                groups[r[key]].append(r)
        for name, rs in groups.items():
            alphas = [r["alpha"] for r in rs if "alpha" in r]
            out.append({"by": key, "name": name, "signals": len(rs),
                        "hit_rate": sum(r["ret"] > 0 for r in rs) / len(rs),
                        "avg_return": sum(r["ret"] for r in rs) / len(rs),
                        "avg_alpha": sum(alphas) / len(alphas) if alphas else None,
                        "pnl": sum(r.get("pnl", 0) for r in rs)})
    return sorted(out, key=lambda g: (g["by"], -(g["avg_alpha"] if g["avg_alpha"] is not None else -9e9)))


# ---------- output ----------
def pct(x):
    return "–" if x is None else f"{x * 100:+.2f}%"


def money(x):
    return "–" if x is None else (f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}")


def num(x):
    return "–" if x is None else f"{x:,.4f}"


SIGNED = {"ret", "spy_ret", "alpha", "avg_return", "avg_alpha", "pnl"}
FMT = {"ret": pct, "spy_ret": pct, "alpha": pct, "avg_return": pct, "avg_alpha": pct, "pnl": money,
       "entry_px": money, "mark_px": money, "qty": num,
       "hit_rate": lambda x: "–" if x is None else f"{x * 100:.0f}%"}


def table(rows, cols: list[tuple[str, str]]) -> str:
    head = "".join(f'<th class="{"num" if c in FMT or c in ("signals", "hold_days") else ""}">{html.escape(label)}</th>'
                   for c, label in cols)
    body = []
    for r in rows:
        cells = []
        for c, _ in cols:
            v = r.get(c)
            text = FMT.get(c, lambda x: "–" if x is None else str(x))(v)
            cls = (" pos" if v > 0 else " neg" if v < 0 else "") if c in SIGNED and isinstance(v, (int, float)) else ""
            num_cls = " num" if c in FMT or c in ("signals", "hold_days") else ""
            cells.append(f'<td class="{cls}{num_cls}">{html.escape(text)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    empty = f'<tr><td colspan="{len(cols)}" class="empty">Nothing yet</td></tr>'
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body) or empty}</tbody></table></div>'


def tile(label, value, cls=""):
    return f'<div class="tile"><span>{html.escape(label)}</span><b class="{cls}">{html.escape(value)}</b></div>'


def render(rows, board, n_skipped, has_keys) -> str:
    measured = [r for r in rows if "ret" in r]
    alphas = [r["alpha"] for r in measured if "alpha" in r]
    pnl = sum(r.get("pnl", 0) for r in measured)
    avg_alpha = sum(alphas) / len(alphas) if alphas else None
    sign = lambda x: "" if not x else ("pos" if x > 0 else "neg")  # noqa: E731
    tiles = "".join([
        tile("Signals traded", f"{sum(r['status'] in ('open', 'closed') for r in rows)} of {len(rows)}"),
        tile("Open / closed", f"{sum(r['status'] == 'open' for r in rows)} / {sum(r['status'] == 'closed' for r in rows)}"),
        tile("Total P&L", money(pnl) if measured else "–", sign(pnl)),
        tile("Hit rate", f"{sum(r['ret'] > 0 for r in measured) / len(measured) * 100:.0f}%" if measured else "–"),
        tile("Avg alpha vs SPY", pct(avg_alpha), sign(avg_alpha)),
        tile("Skipped mentions", str(n_skipped)),
    ])
    warn = "" if has_keys else '<p class="warn">No Alpaca keys: showing planned signals only, no fills or prices.</p>'
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    board_cols = [("by", "Group"), ("name", "Name"), ("signals", "Signals"), ("hit_rate", "Hit rate"),
                  ("avg_return", "Avg return"), ("avg_alpha", "Avg alpha"), ("pnl", "P&L")]
    pos_cols = [("ticker", "Ticker"), ("status", "Status"), ("speaker", "Speaker"), ("podcast", "Podcast"),
                ("entry_date", "Entry"), ("entry_px", "Entry px"), ("qty", "Qty"), ("mark_px", "Mark / exit px"),
                ("ret", "Return"), ("spy_ret", "SPY"), ("alpha", "Alpha"), ("pnl", "P&L"),
                ("hold_days", "Hold days"), ("exit_date", "Exited")]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Trader Report</title>
<style>
:root {{ --bg:#fafaf9; --card:#fff; --fg:#1c1917; --mute:#78716c; --line:#e7e5e4; --pos:#15803d; --neg:#b91c1c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141210; --card:#1c1a17; --fg:#f5f5f4; --mute:#a8a29e; --line:#2e2b27; --pos:#4ade80; --neg:#f87171; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,-apple-system,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0; }} h2 {{ font-size:15px; margin:32px 0 10px; }}
.sub, .note {{ color:var(--mute); }} .note {{ font-size:12px; margin-top:24px; }} .warn {{ color:var(--neg); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-top:20px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.tile span {{ color:var(--mute); font-size:12px; }} .tile b {{ display:block; font-size:22px; font-weight:600; margin-top:2px; }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; text-align:left; }}
th {{ font-size:12px; color:var(--mute); font-weight:600; }} tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; }} .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .empty {{ color:var(--mute); text-align:center; }}
</style></head><body><main>
<h1>Signal Trader</h1><div class="sub">Alpaca paper account · {html.escape(stamp)}</div>{warn}
<div class="tiles">{tiles}</div>
<h2>Leaderboard</h2>{table(board, board_cols)}
<h2>Positions</h2>{table(rows, pos_cols)}
<p class="note">Return is per signal from the entry fill to the exit fill, or to the latest price while open.
SPY is a hypothetical $1,000 purchase at the entry fill time. Alpha = return − SPY return over the same window.
Hit rate = share of signals with a positive return.</p>
</main></body></html>"""


def write_csv(path, rows, cols):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    rules, con = load_rules(), db()
    tc = connect_alpaca(con)
    marks = {}
    if tc is not None:
        record_benchmarks(con, rules)
        marks = {p.symbol: float(p.current_price) for p in tc.get_all_positions()}
        held = {r["ticker"] for r in con.execute(
            "SELECT s.ticker FROM signals s JOIN orders o ON o.mention_id = s.mention_id AND o.role = 'entry' "
            "AND o.filled_at IS NOT NULL")}
        marks.update(latest_prices((held | {rules["benchmark"]["symbol"]}) - set(marks), rules["data_feed"]))
    rows = build_rows(con, marks)
    board = leaderboard(rows)
    n_skipped = con.execute("SELECT COUNT(*) FROM signals WHERE status = 'skipped'").fetchone()[0]

    REPORTS.mkdir(exist_ok=True)
    write_csv(REPORTS / "signals.csv", rows,
              ["episode", "date", "podcast", "source", "speaker", "ticker", "confidence", "status", "entry_date",
               "entry_px", "qty", "mark_px", "exit_date", "ret", "spy_ret", "alpha", "pnl", "hold_days"])
    write_csv(REPORTS / "leaderboard.csv", board,
              ["by", "name", "signals", "hit_rate", "avg_return", "avg_alpha", "pnl"])
    (REPORTS / "report.html").write_text(render(rows, board, n_skipped, tc is not None))
    print(f"wrote {REPORTS / 'report.html'}, signals.csv, leaderboard.csv "
          f"({sum('ret' in r for r in rows)} signals with prices)")


if __name__ == "__main__":
    main()

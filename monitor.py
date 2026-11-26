"""Pull positions and fills from Alpaca, compute P&L and alpha vs the SPY benchmark,
and write reports/report.html plus reports/signals.csv and reports/leaderboard.csv.

    python monitor.py
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
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


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def activity_html(a: dict | None) -> str:
    if not a:
        return '<div class="card empty">No daily activity yet.</div>'
    items = []
    for b in a.get("buys", []):
        why = f"{esc(b['speaker'])} on {esc(b['podcast'])} · {esc(b['confidence'])} confidence · {esc(b.get('reasoning'))}"
        quote = f'<q>{esc(b["quote"])}</q>' if b.get("quote") else ""
        items.append(f'<li><span class="tag buy">BUY</span><b>{esc(b["symbol"])}</b> ${float(b["notional"]):,.0f} '
                     f'<span class="mute">· hold {esc(b["hold_days"])} trading days, exit ≈ {esc(b["est_exit"])} · {esc(b["note"])}</span>'
                     f'<div class="why">{why}{quote}</div></li>')
    for x in a.get("exits", []):
        items.append(f'<li><span class="tag sell">SELL</span><b>{esc(x["symbol"])}</b> '
                     f'<span class="mute">· horizon reached (entered {esc(x["entry_date"])})</span></li>')
    for e in a.get("episodes", []):
        n = f" · {e['mentions']} views" if e.get("mentions") is not None else ""
        items.append(f'<li><span class="tag ep">EPISODE</span><b>{esc(e["podcast"])}</b> {esc(e["title"])} '
                     f'<span class="mute">· {esc(e["date"])}{n} · {esc(e["status"])}</span></li>')
    for err in a.get("errors", []):
        items.append(f'<li><span class="tag err">PROBLEM</span>{esc(err)}</li>')
    if not items:
        items.append('<li class="mute">Nothing new today: no new episodes, trades or exits.</li>')
    return f'<div class="card"><ul class="feed">{"".join(items)}</ul></div>'


def recent_signals_html(con, limit=30) -> str:
    rows = con.execute(
        """SELECT e.date, e.podcast, s.speaker, s.asset, s.ticker_stated, s.sentiment, s.confidence,
                  s.status, s.skip_reason, s.reasoning
           FROM signals s JOIN episodes e ON e.id = s.episode_id
           WHERE s.sentiment IN ('bullish', 'bearish') AND s.skip_reason NOT LIKE 'episode excluded%%'
           ORDER BY e.date DESC, s.status, s.confidence LIMIT ?""".replace("%%", "%"), (limit,)).fetchall()
    if not rows:
        return '<div class="card empty">No signals yet.</div>'
    out = []
    for r in rows:
        dec = ('<span class="tag buy">TRADE</span>' if r["status"] == "eligible"
               else f'<span class="mute small">{esc(r["skip_reason"])}</span>')
        view = f'<span class="{"pos" if r["sentiment"] == "bullish" else "neg"}">{esc(r["sentiment"])}</span> · {esc(r["confidence"])}'
        out.append(f'<tr><td>{esc(r["date"])}</td><td>{esc(r["podcast"])}<div class="mute small">{esc(r["speaker"])}</div></td>'
                   f'<td><b>{esc(r["ticker_stated"] or "")}</b> {esc(r["asset"])}<div class="mute small wrapcell">{esc(r["reasoning"])}</div></td>'
                   f'<td>{view}</td><td>{dec}</td></tr>')
    return ('<div class="wrap"><table><thead><tr><th>Date</th><th>Source / speaker</th><th>View</th>'
            f'<th>Direction</th><th>Decision</th></tr></thead><tbody>{"".join(out)}</tbody></table></div>')


def sources_html(a: dict | None) -> str:
    src = (a or {}).get("sources", [])
    if not src:
        return ""
    chips = "".join(f'<span class="chip {"ok" if s["status"] == "ok" else "bad"}" title="{esc(s["status"])}">'
                    f'{esc(s["name"])} <span class="mute">· latest {esc(s.get("latest") or "–")}</span></span>' for s in src)
    return f'<div class="chips">{chips}</div>'


def render(rows, board, n_skipped, has_keys, con=None, activity=None) -> str:
    measured = [r for r in rows if "ret" in r]
    alphas = [r["alpha"] for r in measured if "alpha" in r]
    pnl = sum(r.get("pnl", 0) for r in measured)
    avg_alpha = sum(alphas) / len(alphas) if alphas else None
    sign = lambda x: "" if not x else ("pos" if x > 0 else "neg")  # noqa: E731
    n_open = sum(r["status"] == "open" for r in rows)
    n_closed = sum(r["status"] == "closed" for r in rows)
    tiles = "".join([
        tile("Total P&L", money(pnl) if measured else "–", sign(pnl)),
        tile("Avg alpha vs SPY", pct(avg_alpha), sign(avg_alpha)),
        tile("Hit rate", f"{sum(r['ret'] > 0 for r in measured) / len(measured) * 100:.0f}%" if measured else "–"),
        tile("Open positions", str(n_open)),
        tile("Closed", str(n_closed)),
        tile("Mentions screened", str(n_skipped + len(rows))),
    ])
    warn = "" if has_keys else '<p class="warn">No Alpaca keys: showing signals only, no fills or prices.</p>'
    stamp = dt.datetime.now().astimezone().strftime("%b %d, %Y · %H:%M %Z")
    board_cols = [("by", "Group"), ("name", "Name"), ("signals", "Signals"), ("hit_rate", "Hit rate"),
                  ("avg_return", "Avg return"), ("avg_alpha", "Avg alpha"), ("pnl", "P&L")]
    pos_cols = [("ticker", "Ticker"), ("status", "Status"), ("speaker", "Speaker"), ("podcast", "Source"),
                ("entry_date", "Entry"), ("entry_px", "Entry px"), ("mark_px", "Last / exit px"),
                ("ret", "Return"), ("spy_ret", "SPY"), ("alpha", "Alpha"), ("pnl", "P&L"), ("hold_days", "Hold days")]
    today = esc((activity or {}).get("date", ""))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Trader</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --fg:#18181b; --mute:#71717a; --line:#e4e4e7; --pos:#15803d; --neg:#b91c1c;
        --accent:#2563eb; --chip-ok:#ecfdf5; --chip-bad:#fef2f2; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111113; --card:#1a1a1d; --fg:#f4f4f5; --mute:#a1a1aa; --line:#2a2a2e;
        --pos:#4ade80; --neg:#f87171; --accent:#60a5fa; --chip-ok:#052e1c; --chip-bad:#3b0d0d; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1120px; margin:0 auto; padding:28px 16px 56px; }}
header {{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }}
h1 {{ font-size:24px; margin:0; letter-spacing:-.01em; }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--mute); margin:36px 0 10px; font-weight:600; }}
.mute {{ color:var(--mute); }} .small {{ font-size:12px; }} .warn {{ color:var(--neg); }}
.badge {{ font-size:12px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--mute); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:20px; }}
.tile, .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; }}
.tile {{ padding:14px 16px; }} .tile span {{ color:var(--mute); font-size:12px; }}
.tile b {{ display:block; font-size:24px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }}
.card {{ padding:4px 16px; }} .card.empty {{ padding:16px; color:var(--mute); }}
.feed {{ list-style:none; margin:0; padding:0; }} .feed li {{ padding:12px 0; border-bottom:1px solid var(--line); }}
.feed li:last-child {{ border-bottom:0; }} .why {{ margin-top:4px; color:var(--mute); }}
q {{ display:block; margin-top:4px; font-style:italic; }}
.tag {{ display:inline-block; font-size:11px; font-weight:600; letter-spacing:.04em; padding:1px 7px; border-radius:6px; margin-right:8px; }}
.tag.buy {{ background:var(--chip-ok); color:var(--pos); }} .tag.sell {{ background:var(--chip-bad); color:var(--neg); }}
.tag.ep {{ background:var(--bg); color:var(--accent); border:1px solid var(--line); }} .tag.err {{ background:var(--chip-bad); color:var(--neg); }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
.chip {{ font-size:12px; padding:4px 10px; border-radius:999px; border:1px solid var(--line); background:var(--card); }}
.chip.ok::before {{ content:"●"; color:var(--pos); margin-right:6px; }} .chip.bad::before {{ content:"●"; color:var(--neg); margin-right:6px; }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:12px; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:10px 14px; border-bottom:1px solid var(--line); white-space:nowrap; text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--mute); font-weight:600; }} tr:last-child td {{ border-bottom:0; }}
.wrapcell {{ white-space:normal; max-width:520px; }}
.num {{ text-align:right; }} .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .empty {{ color:var(--mute); text-align:center; }}
footer {{ font-size:12px; color:var(--mute); margin-top:32px; }}
</style></head><body><main>
<header><h1>Signal Trader</h1><span class="badge">Paper account · updated {esc(stamp)}</span></header>
{warn}{sources_html(activity)}
<div class="tiles">{tiles}</div>
<h2>Today {('· ' + today) if today else ''}</h2>{activity_html(activity)}
<h2>Positions</h2>{table(rows, pos_cols)}
<h2>Leaderboard</h2>{table(board, board_cols)}
<h2>Recent signals</h2>{recent_signals_html(con) if con is not None else ''}
<footer>Return runs from the entry fill to the exit fill, or to the latest price while open. SPY is a hypothetical $1,000
purchase at each entry's fill time; alpha = return − SPY return over the same window. Hit rate = share of signals with a
positive return. Paper trading only; not investment advice.</footer>
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
    try:
        activity = json.loads((REPORTS / "activity.json").read_text())
    except FileNotFoundError:
        activity = None
    (REPORTS / "report.html").write_text(render(rows, board, n_skipped, tc is not None, con, activity))
    print(f"wrote {REPORTS / 'report.html'}, signals.csv, leaderboard.csv "
          f"({sum('ret' in r for r in rows)} signals with prices)")


if __name__ == "__main__":
    main()

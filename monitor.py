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

from portfolio import chart_svg
import portfolio
from common import ROOT, REPORTS, connect_alpaca, db, latest_prices, load_rules, record_benchmarks


def build_rows(con, marks: dict[str, float]) -> list[dict]:
    rows = []
    for s in con.execute(
            """SELECT s.*, e.podcast, e.source, e.date AS ep_date,
                      en.filled_avg_price AS entry_px, en.filled_qty AS qty, en.filled_at AS entry_at, en.status AS entry_status,
                      ex.filled_avg_price AS exit_px, ex.filled_at AS exit_at, ex.status AS exit_status,
                      b.entry_price AS spy_entry, b.exit_price AS spy_exit, b.qty AS spy_qty
               FROM signals s JOIN episodes e ON e.id = s.episode_id
               LEFT JOIN orders en ON en.mention_id = s.mention_id AND en.role = 'entry'
               LEFT JOIN orders ex ON ex.mention_id = s.mention_id AND ex.role = 'exit'
               LEFT JOIN benchmarks b ON b.mention_id = s.mention_id
               WHERE s.status = 'eligible' OR en.client_order_id IS NOT NULL
               ORDER BY e.date, s.ticker"""):
        r = {"episode": s["episode_id"], "date": s["ep_date"], "podcast": s["podcast"], "source": s["source"],
             "speaker": s["speaker"], "ticker": s["ticker"], "confidence": s["confidence"],
             "hold_days": s["hold_days"], "status": "not placed", "book": s["book"], "theme": s["theme"]}
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
            r.update(spy_entry_px=s["spy_entry"], spy_qty=s["spy_qty"], spy_mark_px=spy_mark)
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
    for key in ("book", "theme", "speaker", "source", "podcast"):
        groups = defaultdict(list)
        for r in rows:
            if "ret" in r and r.get(key):
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


def table(rows, cols: list[tuple[str, str]], fmt: dict | None = None) -> str:
    fmt = fmt or FMT
    head = "".join(f'<th class="{"num" if c in FMT or c in ("signals", "hold_days", "ideas") else ""}">{html.escape(label)}</th>'
                   for c, label in cols)
    body = []
    for r in rows:
        cells = []
        for c, _ in cols:
            v = r.get(c)
            text = fmt.get(c, lambda x: "–" if x is None else str(x))(v)
            cls = (" pos" if v > 0 else " neg" if v < 0 else "") if c in SIGNED and isinstance(v, (int, float)) else ""
            num_cls = " num" if c in FMT or c in ("signals", "hold_days", "ideas") else ""
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


def theme_ideas_html(con, mode: str, limit: int = 14) -> str:
    try:
        episodes = json.loads((ROOT / "signals.json").read_text())
    except FileNotFoundError:
        return ""
    state = {(r["episode_id"], r["ticker"]): r for r in con.execute(
        """SELECT s.episode_id, s.ticker, s.status, s.skip_reason, o.status AS order_status
           FROM signals s LEFT JOIN orders o ON o.mention_id = s.mention_id AND o.role = 'entry'
           WHERE s.book = 'theme'""")}
    items = []
    for ep in sorted(episodes, key=lambda e: e["date"], reverse=True):
        for i in ep.get("theme_ideas") or []:
            items.append((ep, i))
    if not items:
        return '<div class="card empty">No theme ideas yet. They appear after the next daily run.</div>'
    out = []
    for ep, i in items[:limit]:
        picks = []
        for t in i["picks"]:
            st = state.get((ep["id"], t))
            if st is None:
                label = "not traded"
            elif st["order_status"]:
                label = "bought"
            elif st["status"] == "eligible":
                label = "shadow" if mode != "paper" else "queued"
            else:
                label = ("too weak to trade" if (st["skip_reason"] or "").startswith("theme idea strength")
                         else st["skip_reason"] or "not traded")
            picks.append(f'<span class="chip"><b>{esc(t)}</b> <span class="mute">· {esc(label)}</span></span>')
        effect = f'<span class="{"pos" if i["effect"] == "tailwind" else "neg"}">{esc(i["effect"])}</span> · {esc(i["strength"])}'
        out.append(f'<tr><td>{esc(ep["date"])}<div class="mute small">{esc(ep["podcast"])}</div></td>'
                   f'<td><b>{esc(i["event"])}</b><div class="mute small wrapcell">{esc(i["causal_chain"])}</div>'
                   f'<div class="mute small wrapcell">Wrong if: {esc(i["invalidation"])}</div></td>'
                   f'<td>{esc(i["theme"].replace("_", " "))}</td><td>{effect}</td><td class="chips-cell">{"".join(picks) or "–"}</td></tr>')
    return ('<div class="wrap"><table><thead><tr><th>Episode</th><th>World event → why</th><th>Theme</th>'
            f'<th>Effect</th><th>Picks</th></tr></thead><tbody>{"".join(out)}</tbody></table></div>')


def portfolio_html(summary, points) -> str:
    if not summary:
        return '<div class="card empty">Starts with the first fills. Every dollar we invest is matched with the same dollars in SPY.</div>'
    sign = lambda v: "pos" if v > 0 else "neg" if v < 0 else ""  # noqa: E731
    stats = "".join([
        tile("Invested (our picks)", money(summary["invested"])),
        tile("Our P&L", f"{money(summary['pnl'])} ({pct(summary['ret'])})", sign(summary["pnl"])),
        tile("Same $ in the S&P 500", f"{money(summary['spy_pnl'])} ({pct(summary['spy_ret'])})", sign(summary["spy_pnl"])),
        tile("Ahead of / behind S&P", f"{money(summary['diff'])} ({pct(summary['excess_ret'])})", sign(summary["diff"])),
    ])
    chart = chart_svg(points)
    legend = ('<div class="legend"><span><i style="background:var(--accent)"></i>Our portfolio</span>'
              '<span><i style="background:var(--mute)"></i>Same dollars in SPY</span> '
              '<span class="mute">cumulative profit and loss, in dollars</span></div>') if chart else ""
    return f'<div class="tiles">{stats}</div>' + (f'<div class="card chart">{legend}{chart}</div>' if chart else "")


def shadow_html(shadow_data, enabled: bool) -> str:
    if not enabled:
        return ""
    if shadow_data is None:
        return '<div class="card empty">The shadow account needs Alpaca keys for price data.</div>'
    rows, summary = shadow_data
    if not summary:
        return '<div class="card empty">No shadow results yet: ideas need a trading day or more to produce a return.</div>'
    sum_cols = [("group", "Group"), ("ideas", "Ideas"), ("hit_rate", "Hit rate"), ("beat_spy", "Beat SPY"),
                ("avg_return", "Avg return"), ("avg_alpha", "Avg alpha"), ("pnl", "Hypothetical P&L")]
    fmt = {**FMT, "beat_spy": FMT["hit_rate"]}
    t1 = table(summary, sum_cols, fmt)
    pick_cols = [("ep_date", "Episode"), ("ticker", "Pick"), ("theme", "Theme"), ("strength", "Strength"),
                 ("status", "Status"), ("entry_date", "Entry"), ("ret", "Return"), ("spy_ret", "SPY"),
                 ("alpha", "Alpha"), ("pnl", "P&L")]
    recent = sorted([r for r in rows if "ret" in r], key=lambda r: r["ep_date"], reverse=True)[:15]
    t2 = table([{**r, "theme": r["theme"].replace("_", " ")} for r in recent], pick_cols, fmt)
    return t1 + '<div style="height:12px"></div>' + t2


def sources_html(a: dict | None) -> str:
    src = (a or {}).get("sources", [])
    if not src:
        return ""
    chips = "".join(f'<span class="chip {"ok" if s["status"] == "ok" else "bad"}" title="{esc(s["status"])}">'
                    f'{esc(s["name"])} <span class="mute">· latest {esc(s.get("latest") or "–")}</span></span>' for s in src)
    return f'<div class="chips">{chips}</div>'


def render(rows, board, n_skipped, has_keys, con=None, activity=None, shadow_data=None, shadow_on=False,
           portfolio_data=None) -> str:
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
    pos_cols = [("ticker", "Ticker"), ("book", "Book"), ("status", "Status"), ("speaker", "Speaker"), ("podcast", "Source"),
                ("entry_date", "Entry"), ("entry_px", "Entry px"), ("mark_px", "Last / exit px"),
                ("ret", "Return"), ("spy_ret", "SPY"), ("alpha", "Alpha"), ("pnl", "P&L"), ("hold_days", "Hold days")]
    today = esc((activity or {}).get("date", ""))
    rules_ = load_rules()
    mode = (rules_.get("thematic") or {}).get("mode", "shadow")
    shadow_notional = (rules_.get("shadow") or {}).get("notional_usd", 500)
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
.chart {{ padding:12px 16px; margin-top:12px; }} .axis {{ font-size:11px; fill:var(--mute); }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; font-size:12px; margin-bottom:6px; }}
.legend i {{ display:inline-block; width:14px; height:3px; margin-right:6px; vertical-align:middle; border-radius:2px; }}
.chips-cell {{ white-space:normal; min-width:170px; }} .chips-cell .chip {{ display:inline-block; margin:0 6px 4px 0; }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
.chip {{ font-size:12px; padding:4px 10px; border-radius:999px; border:1px solid var(--line); background:var(--card); }}
.chip.ok::before {{ content:"●"; color:var(--pos); margin-right:6px; }} .chip.bad::before {{ content:"●"; color:var(--neg); margin-right:6px; }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:12px; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:10px 14px; border-bottom:1px solid var(--line); white-space:nowrap; text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--mute); font-weight:600; }} tr:last-child td {{ border-bottom:0; }}
.wrapcell {{ white-space:normal; max-width:400px; }}
.num {{ text-align:right; }} .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .empty {{ color:var(--mute); text-align:center; }}
footer {{ font-size:12px; color:var(--mute); margin-top:32px; }}
</style></head><body><main>
<header><h1>Signal Trader</h1><span class="badge">Paper account · updated {esc(stamp)}</span></header>
{warn}{sources_html(activity)}
<h2>Portfolio vs the S&amp;P 500 · same dollars, same days</h2>{portfolio_html(*(portfolio_data or (None, [])))}
<div class="tiles">{tiles}</div>
<h2>Today {('· ' + today) if today else ''}</h2>{activity_html(activity)}
<h2>Positions</h2>{table(rows, pos_cols)}
<h2>Theme ideas · {esc(mode)} mode</h2>{theme_ideas_html(con, mode) if con is not None else ''}
{('<h2>Shadow account · every theme idea, hypothetical $' + str(int(shadow_notional)) + ' each</h2>' + shadow_html(shadow_data, shadow_on)) if shadow_on else ''}
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
    portfolio_data = (portfolio.summary(rows), [])
    if tc is not None and portfolio_data[0]:
        try:
            import shadow
            lots_ = portfolio.lots(rows)
            first = min(dt.date.fromisoformat(r["entry_date"]) for r in lots_)
            syms = {r["ticker"] for r in lots_} | {rules["benchmark"]["symbol"]}
            bars_ = shadow.fetch_bars(syms, first, dt.date.today(), rules["data_feed"])
            portfolio_data = (portfolio_data[0], portfolio.series(rows, bars_, dt.date.today(), rules["benchmark"]["symbol"]))
        except Exception as exc:
            print(f"portfolio chart skipped: {type(exc).__name__}: {str(exc)[:200]}")
        ps = portfolio_data[0]
        print(f"  portfolio: invested ${ps['invested']:,.0f}, P&L ${ps['pnl']:+,.2f} ({ps['ret']:+.2%}); "
              f"same $ in SPY ${ps['spy_pnl']:+,.2f} ({ps['spy_ret']:+.2%}); difference ${ps['diff']:+,.2f}")
    shadow_on = bool((rules.get("shadow") or {}).get("enabled"))
    shadow_data = None
    if shadow_on and tc is not None:
        import shadow
        try:
            picks = shadow.shadow_picks(con)
            if picks:
                start = min(dt.date.fromisoformat(p["ep_date"]) for p in picks)
                syms = {p["ticker"] for p in picks} | {rules["benchmark"]["symbol"]}
                bars = shadow.fetch_bars(syms, start, dt.date.today(), rules["data_feed"])
                srows = shadow.evaluate(picks, bars, dt.date.today(), rules["benchmark"]["symbol"],
                                        float(rules["shadow"]["notional_usd"]))
                shadow_data = (srows, shadow.summarize(srows))
                for g in shadow_data[1]:
                    print(f"  shadow {g['group']:<22} ideas {g['ideas']:>3}  hit {g['hit_rate']:.0%}  "
                          f"avg return {g['avg_return']:+.2%}  avg alpha {g['avg_alpha']:+.2%}")
                write_csv(REPORTS / "shadow.csv", srows,
                          ["ep_date", "podcast", "ticker", "theme", "strength", "traded", "status", "entry_date",
                           "exit_date", "entry_px", "mark_px", "ret", "spy_ret", "alpha", "pnl", "event"])
                write_csv(REPORTS / "shadow_summary.csv", shadow_data[1],
                          ["group", "ideas", "hit_rate", "beat_spy", "avg_return", "avg_alpha", "pnl"])
            else:
                shadow_data = ([], [])
        except Exception as exc:  # the dashboard must still build if price data is unavailable
            print(f"shadow account skipped: {type(exc).__name__}: {str(exc)[:200]}")
    try:
        activity = json.loads((REPORTS / "activity.json").read_text())
    except FileNotFoundError:
        activity = None
    (REPORTS / "report.html").write_text(render(rows, board, n_skipped, tc is not None, con, activity, shadow_data, shadow_on, portfolio_data))
    print(f"wrote {REPORTS / 'report.html'}, signals.csv, leaderboard.csv "
          f"({sum('ret' in r for r in rows)} signals with prices)")


if __name__ == "__main__":
    main()

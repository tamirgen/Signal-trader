"""Daily "what happened and why" report: new episodes, the signals found in them, trades placed
(with the speaker's reasoning), signals not traded (with the rule that stopped them), and exits.

Writes activity/YYYY-MM-DD.md (kept in the repo as a history), reports/activity.json for the
dashboard, and the GitHub Actions run summary when running in Actions.

    python activity.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

from common import ET, REPORTS, ROOT, db

ACTIVITY = ROOT / "activity"


def read_csv(path):
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def build() -> dict:
    today = dt.datetime.now(ET).date().isoformat()
    try:
        fetch = json.loads((REPORTS / "fetch_log.json").read_text())
    except FileNotFoundError:
        fetch = {"sources": [], "episodes": [], "errors": []}
    con = db()
    plan = read_csv(REPORTS / "planned_orders.csv")
    exits = read_csv(REPORTS / "exits.csv")

    new_ids = [e["id"] for e in fetch.get("episodes", []) if e.get("id")]
    signals = []
    if new_ids:
        q = ",".join("?" * len(new_ids))
        signals = [dict(r) for r in con.execute(
            f"""SELECT s.episode_id, e.podcast, e.title, s.asset, s.ticker, s.ticker_stated, s.sentiment, s.confidence,
                       s.speaker, s.horizon, s.hold_days, s.reasoning, s.quote, s.status, s.skip_reason
                FROM signals s JOIN episodes e ON e.id = s.episode_id
                WHERE s.episode_id IN ({q}) ORDER BY s.status, s.episode_id""", new_ids)]
    buys = [r for r in plan if r.get("action") == "BUY"]
    shadow = [r for r in plan if r.get("action") == "SHADOW"]
    ideas = []
    try:
        for ep in json.loads((ROOT / "signals.json").read_text()):
            if ep["id"] in new_ids:
                ideas += [{**i, "episode": ep["id"], "podcast": ep["podcast"], "date": ep["date"]}
                          for i in ep.get("theme_ideas") or []]
    except FileNotFoundError:
        pass
    return {
        "date": today,
        "generated": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "sources": fetch.get("sources", []),
        "episodes": fetch.get("episodes", []),
        "new_signals": signals,
        "buys": buys,
        "shadow": shadow,
        "theme_ideas": ideas,
        "skipped_orders": [r for r in plan if r.get("action") == "SKIP"],
        "exits": exits,
        "errors": fetch.get("errors", []),
        "market_open_today": os.environ.get("MARKET_STATUS", "open") != "closed",
    }


def to_markdown(a: dict) -> str:
    L = [f"# Signal Trader — {a['date']}", "", f"_Generated {a['generated']}_", ""]

    L += ["## Sources checked", "", "| Source | Status | Latest episode | New |", "|---|---|---|---|"]
    for s in a["sources"]:
        L.append(f"| {s['name']} | {s['status']} | {s.get('latest') or '–'} | {s.get('new', 0)} |")

    L += ["", "## New episodes", ""]
    if not a["episodes"]:
        L.append("No new episodes today.")
    for e in a["episodes"]:
        link = f" ([link]({e['link']}))" if e.get("link") else ""
        extra = f", {e['mentions']} views extracted" if e.get("mentions") is not None else ""
        L.append(f"- **{e['podcast']}**: {e['title']} ({e['date']}){link} — {e['status']}"
                 f" · transcript: {e.get('transcript_source', '–')}{extra}")

    L += ["", "## Trades placed", ""]
    if not a["buys"]:
        L.append("No new trades." if a["market_open_today"] else "Market closed today — no trading.")
    for b in a["buys"]:
        who = (f"Theme logic ({b['theme']}) from {b['podcast']}" if b.get("book") == "theme"
               else f"{b['speaker']} ({b['podcast']})")
        L += [f"### BUY {b['symbol']} — ${float(b['notional']):,.0f}" + (" · theme" if b.get("book") == "theme" else ""),
              f"- **Why:** {who}, {b['confidence']} confidence, bullish. {b.get('reasoning') or ''}",
              *( [f"- **Quote:** “{b['quote']}”"] if b.get("quote") else [] ),
              f"- **Size:** ${float(b['notional']):,.0f} now; {b.get('n_sources', 1)} source(s) back it "
              f"({b.get('sources') or b['podcast']}), so the target is ${float(b.get('target') or b['notional']):,.0f} "
              f"(already held ${float(b.get('held') or 0):,.0f}; cap $1,000 per position)",
              f"- **Plan:** hold {b['hold_days']} trading days ({b['bucket']} horizon: “{b['horizon']}”), exit around {b['est_exit']}",
              f"- **Status:** {b['note'] or 'planned'}", ""]

    if a["skipped_orders"]:
        L += ["## Eligible but not traded", ""]
        L += [f"- {r['symbol']}: {r['note']}" for r in a["skipped_orders"]]
        L.append("")

    L += ["## Exits", ""]
    L += [f"- {x['action']} {x['symbol']} qty {float(x['qty']):.4f} (entered {x['entry_date']}, horizon reached {x['exit_on']})"
          for x in a["exits"]] or ["No positions reached their horizon today."]

    traded = [s for s in a["new_signals"] if s["status"] == "eligible"]
    skipped = [s for s in a["new_signals"] if s["status"] == "skipped"]
    if a["new_signals"]:
        L += ["", "## Signals found in new episodes", "",
              f"{len(traded)} eligible, {len(skipped)} not traded.", "",
              "| Asset | Ticker | View | Speaker | Decision |", "|---|---|---|---|---|"]
        for s in traded + skipped:
            decision = "✅ trade" if s["status"] == "eligible" else f"skip — {s['skip_reason']}"
            L.append(f"| {s['asset']} | {s['ticker_stated'] or '–'} | {s['sentiment']}, {s['confidence']} "
                     f"| {s['speaker']} | {decision} |")

    if a["errors"]:
        L += ["", "## Problems", ""] + [f"- ⚠️ {e}" for e in a["errors"]]
    return "\n".join(L) + "\n"


def main():
    a = build()
    md = to_markdown(a)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "activity.json").write_text(json.dumps(a, indent=1, default=str))
    ACTIVITY.mkdir(exist_ok=True)
    (ACTIVITY / f"{a['date']}.md").write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    print(md)


if __name__ == "__main__":
    main()

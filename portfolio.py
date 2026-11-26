"""Portfolio vs the S&P 500, in dollars.

Every dollar invested in an entry is matched by the same dollars in SPY at the same fill time (and sold at the
same time as our exit), so the totals are directly comparable: "our $3,000 vs $3,000 of SPY bought the same day".
"""
from __future__ import annotations

import datetime as dt

LOT_KEYS = ("entry_px", "qty", "mark_px", "spy_entry_px", "spy_qty", "spy_mark_px")


def lots(rows: list[dict]) -> list[dict]:
    return [r for r in rows if all(r.get(k) for k in LOT_KEYS)]


def summary(rows: list[dict]) -> dict | None:
    ls = lots(rows)
    if not ls:
        return None
    invested = sum(r["qty"] * r["entry_px"] for r in ls)
    value = sum(r["qty"] * r["mark_px"] for r in ls)
    spy_invested = sum(r["spy_qty"] * r["spy_entry_px"] for r in ls)
    spy_value = sum(r["spy_qty"] * r["spy_mark_px"] for r in ls)
    return {"positions": len(ls), "invested": invested, "value": value, "pnl": value - invested,
            "ret": value / invested - 1, "spy_invested": spy_invested, "spy_value": spy_value,
            "spy_pnl": spy_value - spy_invested, "spy_ret": spy_value / spy_invested - 1,
            "diff": (value - invested) - (spy_value - spy_invested),
            "excess_ret": (value / invested - 1) - (spy_value / spy_invested - 1)}


def series(rows: list[dict], bars: dict, today: dt.date, spy: str = "SPY") -> list[dict]:
    """Cumulative P&L in dollars per trading day, for our lots and for the matched SPY lots."""
    ls = lots(rows)
    if not ls:
        return []
    first = min(r["entry_date"] for r in ls)
    days = sorted(d for d in (bars.get(spy) or {}) if first <= d <= today.isoformat())

    def close(sym: str, day: str):
        series_ = {d: v[1] for d, v in (bars.get(sym) or {}).items() if d <= day}
        return series_[max(series_)] if series_ else None

    out = []
    for d in days:
        ours = theirs = 0.0
        for r in ls:
            if r["entry_date"] > d:
                continue
            done = r.get("exit_date") and r["exit_date"] <= d
            px = r["mark_px"] if done else close(r["ticker"], d)
            sx = r["spy_mark_px"] if done else close(spy, d)
            if px:
                ours += r["qty"] * (px - r["entry_px"])
            if sx:
                theirs += r["spy_qty"] * (sx - r["spy_entry_px"])
        out.append({"date": d, "ours": ours, "spy": theirs})
    return out


def chart_svg(points: list[dict], w: int = 760, h: int = 230) -> str:
    """Two-line SVG chart (ours vs the S&P twin). Colours come from the page's CSS variables."""
    if len(points) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 56, 12, 14, 26
    ys = [p[k] for p in points for k in ("ours", "spy")] + [0.0]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.08, hi + span * 0.08

    def x(i):
        return pad_l + (w - pad_l - pad_r) * i / (len(points) - 1)

    def y(v):
        return pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))

    def line(key, color, width):
        pts = " ".join(f"{x(i):.1f},{y(p[key]):.1f}" for i, p in enumerate(points))
        return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round"/>'

    def money(v):
        return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"

    marks, used = [], []
    for v in (0.0, hi - (hi - lo) * 0.08, lo + (hi - lo) * 0.08):   # zero line first, then top and bottom
        if lo <= v <= hi and all(abs(y(v) - u) >= 18 for u in used):
            used.append(y(v))
            marks.append(v)
    grid = "".join(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--line)"/>'
                   f'<text x="{pad_l - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="axis">{money(v)}</text>'
                   for v in marks)
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Cumulative profit and loss: our portfolio vs the same dollars in SPY" '
            f'style="width:100%;height:auto">{grid}'
            f'{line("spy", "var(--mute)", 2)}{line("ours", "var(--accent)", 2.5)}'
            f'<text x="{pad_l}" y="{h - 6}" class="axis">{points[0]["date"]}</text>'
            f'<text x="{w - pad_r}" y="{h - 6}" text-anchor="end" class="axis">{points[-1]["date"]}</text></svg>')

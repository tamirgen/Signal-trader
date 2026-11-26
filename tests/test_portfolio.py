"""Tests for the dollar-matched portfolio vs S&P comparison. No network."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portfolio  # noqa: E402


def lot(ticker, entry_px, qty, mark_px, spy_entry, spy_mark, entry_date="2026-11-27", exit_date=None):
    notional = entry_px * qty
    return {"ticker": ticker, "entry_px": entry_px, "qty": qty, "mark_px": mark_px, "entry_date": entry_date,
            "exit_date": exit_date, "spy_entry_px": spy_entry, "spy_qty": notional / spy_entry, "spy_mark_px": spy_mark}


class Summary(unittest.TestCase):
    def test_same_dollars_in_spy(self):
        rows = [lot("A", 100, 5, 110, 600, 612), lot("B", 50, 10, 45, 600, 612)]  # $500 each
        s = portfolio.summary(rows)
        self.assertAlmostEqual(s["invested"], 1000)
        self.assertAlmostEqual(s["spy_invested"], 1000)          # matched dollars
        self.assertAlmostEqual(s["pnl"], 50 - 50)                # +$50 and -$50
        self.assertAlmostEqual(s["spy_pnl"], 1000 * 0.02)        # +2%
        self.assertAlmostEqual(s["diff"], -20)
        self.assertAlmostEqual(s["excess_ret"], -0.02)

    def test_nothing_filled_yet(self):
        self.assertIsNone(portfolio.summary([{"ticker": "A", "status": "not placed"}]))


class Series(unittest.TestCase):
    BARS = {"A": {"2026-11-27": (100, 101), "2026-11-30": (102, 105), "2026-12-01": (106, 110)},
            "SPY": {"2026-11-27": (600, 601), "2026-11-30": (602, 606), "2026-12-01": (607, 612)}}

    def test_open_lot_marks_to_each_days_close(self):
        pts = portfolio.series([lot("A", 100, 5, 110, 600, 612)], self.BARS, dt.date(2026, 12, 1))
        self.assertEqual([p["date"] for p in pts], ["2026-11-27", "2026-11-30", "2026-12-01"])
        self.assertAlmostEqual(pts[0]["ours"], 5 * (101 - 100))
        self.assertAlmostEqual(pts[-1]["ours"], 5 * (110 - 100))
        self.assertAlmostEqual(pts[-1]["spy"], (500 / 600) * (612 - 600))

    def test_closed_lot_stops_moving_after_exit(self):
        row = lot("A", 100, 5, 104, 600, 603, exit_date="2026-11-30")
        pts = portfolio.series([row], self.BARS, dt.date(2026, 12, 1))
        self.assertAlmostEqual(pts[1]["ours"], 5 * 4)
        self.assertAlmostEqual(pts[2]["ours"], 5 * 4)            # exit price held, not the later close of 110

    def test_lot_is_ignored_before_it_was_bought(self):
        row = lot("A", 100, 5, 110, 600, 612, entry_date="2026-11-30")
        pts = portfolio.series([row], self.BARS, dt.date(2026, 12, 1))
        self.assertEqual(pts[0]["date"], "2026-11-30")

    def test_chart_needs_two_points(self):
        self.assertEqual(portfolio.chart_svg([]), "")
        pts = [{"date": "2026-11-27", "ours": 0, "spy": 0}, {"date": "2026-11-30", "ours": 10, "spy": 4}]
        self.assertIn("<polyline", portfolio.chart_svg(pts))


if __name__ == "__main__":
    unittest.main()

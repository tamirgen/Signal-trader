"""Tests for the shadow account's hypothetical entries, exits and summaries. No network: prices are fake."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import shadow  # noqa: E402

D = dt.date.fromisoformat


def pick(ticker="CRWD", ep_date="2026-11-17", hold=3, strength="high", traded=False):
    return {"mention_id": "m", "ticker": ticker, "theme": "cybersecurity", "strength": strength, "hold_days": hold,
            "event": "e", "ep_date": ep_date, "podcast": "P", "traded": traded}


# Tue 11-17 episode -> entry at Wed 11-18 open; 3 trading days later (11-19, 11-20, 11-23) -> exit Mon 11-23 open
BARS = {
    "CRWD": {"2026-11-18": (100.0, 101.0), "2026-11-23": (110.0, 111.0), "2026-11-24": (112.0, 120.0)},
    "SPY": {"2026-11-18": (600.0, 601.0), "2026-11-23": (606.0, 607.0), "2026-11-24": (609.0, 610.0)},
}


class Evaluate(unittest.TestCase):
    def setUp(self):
        common._alpaca_days = None

    def test_closed_pick_uses_entry_open_and_exit_open(self):
        [r] = shadow.evaluate([pick()], BARS, D("2026-11-25"))
        self.assertEqual((r["entry_date"], r["exit_date"], r["status"]), ("2026-11-18", "2026-11-23", "closed"))
        self.assertAlmostEqual(r["ret"], 0.10)
        self.assertAlmostEqual(r["spy_ret"], 0.01)
        self.assertAlmostEqual(r["alpha"], 0.09)
        self.assertAlmostEqual(r["pnl"], 50.0)

    def test_open_pick_is_marked_at_the_latest_close(self):
        [r] = shadow.evaluate([pick(hold=60)], BARS, D("2026-11-25"))
        self.assertEqual(r["status"], "open")
        self.assertAlmostEqual(r["ret"], 120 / 100 - 1)
        self.assertAlmostEqual(r["spy_ret"], 610 / 600 - 1)

    def test_pick_whose_entry_is_in_the_future_is_pending(self):
        [r] = shadow.evaluate([pick(ep_date="2026-11-24")], BARS, D("2026-11-24"))
        self.assertEqual(r["status"], "pending")
        self.assertNotIn("ret", r)

    def test_missing_prices_are_reported_not_invented(self):
        [r] = shadow.evaluate([pick(ticker="ZZZZ")], BARS, D("2026-11-25"))
        self.assertEqual(r["status"], "no data")
        self.assertNotIn("ret", r)


class Summary(unittest.TestCase):
    def test_groups_by_strength_and_traded(self):
        rows = shadow.evaluate([pick(strength="high", traded=True), pick(strength="low"), pick(ticker="ZZZZ")],
                               BARS, D("2026-11-25"))
        got = {g["group"]: g for g in shadow.summarize(rows)}
        self.assertEqual(got["All ideas"]["ideas"], 2)          # the no-data pick is left out
        self.assertEqual(got["Strength: high"]["ideas"], 1)
        self.assertEqual(got["Traded for real"]["ideas"], 1)
        self.assertEqual(got["Not traded for real"]["ideas"], 1)
        self.assertAlmostEqual(got["All ideas"]["hit_rate"], 1.0)

    def test_empty_is_fine(self):
        self.assertEqual(shadow.summarize([]), [])


if __name__ == "__main__":
    unittest.main()

"""Tests for position sizing: tiers by number of sources, doubling down, and the per-ticker cap.
Run: python -m unittest -v"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import ingest  # noqa: E402
import trade  # noqa: E402
from test_themes import idea  # noqa: E402

SIZING = common.load_rules()["sizing"]


def call(ep_id, date, podcast, ticker="NVDA", confidence="medium", ideas=None):
    return {"id": ep_id, "podcast": podcast, "source": "S", "date": date, "summary": "",
            "mentions": [{"asset": ticker, "sentiment": "bullish", "confidence": confidence, "ticker_stated": ticker,
                          "speaker": "A", "horizon": "medium", "claim_type": "opinion", "reasoning": "r"}],
            "theme_ideas": ideas or []}


class Tiers(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual([trade.target_usd(SIZING, n) for n in (1, 2, 3, 4, 9)], [500, 1000, 1000, 1000, 1000])

    def test_cap_always_wins(self):
        capped = {**SIZING, "per_position_cap_usd": 600}
        self.assertEqual(trade.target_usd(capped, 3), 600)


class Planning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SIGNAL_DB": str(Path(self.tmp.name) / "t.db")})
        env.start()
        self.addCleanup(env.stop)
        common._alpaca_days = None
        self.rules = common.load_rules()

    def plan(self, episodes, today="2026-11-24"):
        path = Path(self.tmp.name) / "signals.json"
        path.write_text(json.dumps(episodes))
        ingest.ingest(path, self.rules)
        con = common.db()
        with mock.patch.object(trade, "current_session", return_value=dt.date.fromisoformat(today)):
            return trade.plan_entries(con, self.rules, None)

    def buys(self, plan, ticker="NVDA"):
        return [(r["notional"], r["n_sources"]) for r in plan if r["symbol"] == ticker and r["action"] == "BUY"]

    def test_one_source_buys_the_base_amount(self):
        self.assertEqual(self.buys(self.plan([call("a", "2026-11-20", "Show A")])), [(500, 1)])

    def test_second_source_doubles_down(self):
        plan = self.plan([call("a", "2026-11-18", "Show A"), call("b", "2026-11-20", "Show B")])
        self.assertEqual(self.buys(plan), [(500, 1), (500, 2)])  # $1,000 in total

    def test_third_source_does_not_exceed_the_cap(self):
        plan = self.plan([call("a", "2026-11-16", "Show A"), call("b", "2026-11-18", "Show B"),
                          call("c", "2026-11-20", "Show C")])
        got = self.buys(plan)
        self.assertEqual(got, [(500, 1), (500, 2)])  # the third source adds nothing: already at $1,000
        self.assertEqual(sum(n for n, _ in got), 1000)
        self.assertIn("already hold $1,000", [r["note"] for r in plan if r["action"] == "SKIP"][0])

    def test_fourth_source_cannot_exceed_the_cap(self):
        plan = self.plan([call(k, f"2026-11-{15 + i}", f"Show {k}") for i, k in enumerate("abcd")])
        self.assertEqual(sum(n for n, _ in self.buys(plan)), 1000)
        self.assertIn("already hold $1,000", [r["note"] for r in plan if r["action"] == "SKIP"][0])

    def test_same_show_twice_is_one_source(self):
        plan = self.plan([call("a", "2026-11-18", "Show A"), call("b", "2026-11-20", "Show A")])
        self.assertEqual(self.buys(plan), [(500, 1)])

    def test_sources_outside_the_window_do_not_count(self):
        plan = self.plan([call("a", "2026-10-01", "Show A"), call("b", "2026-11-20", "Show B")])
        self.assertEqual(self.buys(plan), [(500, 1)])  # no top-up: the 10-01 source is too old to corroborate

    def test_both_books_share_one_cap(self):
        # one show names NVDA and the theme logic also picks it: still $500 in total, one order
        ep = call("a", "2026-11-20", "Show A", ideas=[idea(theme="semiconductors_ai_compute", picks=["NVDA"])])
        plan = self.plan([ep])
        self.assertEqual(sum(n for n, _ in self.buys(plan)), 500)
        self.assertEqual([r["book"] for r in plan if r["action"] == "BUY" and r["symbol"] == "NVDA"], ["speaker"])

    def test_cap_holds_across_books_with_many_sources(self):
        eps = [call(k, f"2026-11-{15 + i}", f"Show {k}",
                    ideas=[idea(theme="semiconductors_ai_compute", picks=["NVDA"])]) for i, k in enumerate("abcd")]
        plan = self.plan(eps)
        self.assertLessEqual(sum(r["notional"] for r in plan if r["action"] == "BUY" and r["symbol"] == "NVDA"), 1000)


if __name__ == "__main__":
    unittest.main()

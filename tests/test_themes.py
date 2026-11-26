"""Tests for the theme logic: validation, ingest into the theme book, and the position limits.
Run: python -m unittest -v"""
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import ingest  # noqa: E402
import themes  # noqa: E402
import trade  # noqa: E402

UNIVERSE = themes.load_universe()
RULES = common.load_rules()


def idea(**kw):
    base = {"theme": "defense_aerospace", "event": "Taiwan tension rises", "effect": "tailwind",
            "strength": "high", "horizon": "medium", "causal_chain": "tension -> budgets -> defense primes",
            "invalidation": "de-escalation", "picks": ["LMT", "ITA"], "speakers": "Host"}
    base.update(kw)
    return base


class Validation(unittest.TestCase):
    def test_universe_is_well_formed(self):
        self.assertGreaterEqual(len(UNIVERSE), 10)
        for key, t in UNIVERSE.items():
            self.assertTrue(t["description"], key)
            for sym in (t.get("etfs") or []) + (t.get("stocks") or []):
                self.assertRegex(sym, r"^[A-Z]{1,5}(\.[A-Z])?$", key)

    def test_picks_outside_the_theme_are_dropped_and_etf_comes_first(self):
        v = themes.validate_idea(idea(picks=["LMT", "TSLA", "ITA", "NOC"]), UNIVERSE, max_picks=2)
        self.assertEqual(v["picks"], ["ITA", "LMT"])

    def test_unknown_theme_is_rejected(self):
        self.assertIsNone(themes.validate_idea(idea(theme="made_up"), UNIVERSE))

    def test_headwind_has_no_picks(self):
        self.assertEqual(themes.validate_idea(idea(effect="headwind"), UNIVERSE)["picks"], [])

    def test_lowercase_and_duplicate_picks(self):
        self.assertEqual(themes.validate_idea(idea(picks=["ita", "ITA", "lmt"]), UNIVERSE)["picks"], ["ITA", "LMT"])


def episode(ep_id="ep1", date="2026-11-23", ideas=None, podcast="All-In"):
    return {"id": ep_id, "podcast": podcast, "source": "S", "date": date, "summary": "", "mentions": [],
            "theme_ideas": ideas if ideas is not None else []}


class IngestAndLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SIGNAL_DB": str(Path(self.tmp.name) / "t.db")})
        env.start()
        self.addCleanup(env.stop)
        common._alpaca_days = None
        self.rules = common.load_rules()
        self.rules["thematic"].update(mode="paper", max_total_usd=3000,
                                      max_positions_per_theme=2, max_age_days=7)

    def load(self, episodes):
        import json
        path = Path(self.tmp.name) / "signals.json"
        path.write_text(json.dumps(episodes))
        ingest.ingest(path, self.rules)
        return common.db()

    def plan(self, con, today="2026-11-24"):
        d = dt.date.fromisoformat(today)
        with mock.patch.object(trade, "current_session", return_value=d):
            return trade.plan_entries(con, self.rules, None)

    def test_tailwind_picks_become_theme_signals(self):
        con = self.load([episode(ideas=[idea(), idea(effect="headwind", theme="airlines_transport", picks=[])])])
        rows = con.execute("SELECT * FROM signals WHERE book = 'theme' ORDER BY ticker").fetchall()
        self.assertEqual([r["ticker"] for r in rows], ["ITA", "LMT"])
        self.assertTrue(all(r["status"] == "eligible" and r["theme"] == "defense_aerospace" for r in rows))
        self.assertEqual(rows[0]["hold_days"], 60)

    def test_weak_ideas_are_logged_not_traded(self):
        con = self.load([episode(ideas=[idea(strength="low")])])
        rows = con.execute("SELECT status, skip_reason FROM signals WHERE book = 'theme'").fetchall()
        self.assertTrue(rows and all(r["status"] == "skipped" and "strength low" in r["skip_reason"] for r in rows))

    def test_same_ticker_from_two_ideas_in_one_episode_is_one_row(self):
        con = self.load([episode(ideas=[idea(strength="medium", picks=["LMT"]),
                                        idea(strength="high", picks=["LMT"], event="Other")])])
        rows = con.execute("SELECT confidence FROM signals WHERE book = 'theme' AND status = 'eligible'").fetchall()
        self.assertEqual([r["confidence"] for r in rows], ["high"])

    def test_excluded_episode_gets_no_theme_rows(self):
        con = self.load([episode(date="2026-11-20", podcast="Table Four", ideas=[idea()])])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM signals WHERE book = 'theme'").fetchone()[0], 0)

    def test_order_ids_differ_between_books_and_repeat_runs_are_stable(self):
        ep = episode(ideas=[idea(picks=["LMT"])])
        ep["mentions"] = [{"asset": "Lockheed", "sentiment": "bullish", "confidence": "high", "ticker_stated": "LMT",
                           "speaker": "A", "horizon": "medium"}]
        con = self.load([ep])
        ids = {r["book"]: common.entry_coid(r) for r in con.execute("SELECT * FROM signals WHERE ticker = 'LMT'")
               if r["status"] == "eligible"}
        self.assertEqual(ids["speaker"], "ep1-LMT")
        self.assertEqual(ids["theme"], "ep1-T-LMT")
        self.load([ep])  # ingest again: no unique-index error, same rows
        self.assertEqual(con.execute("SELECT COUNT(*) FROM signals WHERE ticker = 'LMT'").fetchone()[0], 2)

    def test_shadow_mode_places_nothing(self):
        self.rules["thematic"]["mode"] = "shadow"
        con = self.load([episode(ideas=[idea()])])
        actions = [r["action"] for r in self.plan(con) if r["book"] == "theme"]
        self.assertEqual(actions, ["SHADOW", "SHADOW"])

    def test_stale_ideas_are_skipped(self):
        con = self.load([episode(date="2026-11-02", ideas=[idea()])])
        plan = [r for r in self.plan(con) if r["book"] == "theme"]
        self.assertTrue(plan and all(r["action"] == "SKIP" and "stale" in r["note"] for r in plan))

    def test_per_theme_limit(self):
        eps = [episode(ep_id=f"e{k}", ideas=[idea(theme="semiconductors_ai_compute", picks=[t])])
               for k, t in enumerate(["SMH", "NVDA", "AMD"])]
        con = self.load(eps)
        plan = [r for r in self.plan(con) if r["book"] == "theme"]
        self.assertEqual([r["action"] for r in plan], ["BUY", "BUY", "SKIP"])
        self.assertIn("theme limit", plan[2]["note"])

    def test_total_limit(self):
        # 6 positions of $500 fit in $3000, the 7th does not
        themes_list = ["defense_aerospace", "oil_gas_producers", "gold_safe_haven", "cybersecurity", "nuclear_uranium",
                       "airlines_transport", "reshoring_infrastructure"]
        eps = [episode(ep_id=f"x{k}", ideas=[idea(theme=t, picks=[UNIVERSE[t]["etfs"][0]])]) for k, t in enumerate(themes_list)]
        plan = [r for r in self.plan(self.load(eps)) if r["book"] == "theme"]
        self.assertEqual([r["action"] for r in plan].count("BUY"), 6)
        self.assertIn("total theme limit", plan[-1]["note"])

    def test_already_holding_the_ticker_blocks_a_repeat_idea(self):
        eps = [episode(ep_id="a1", date="2026-11-20", ideas=[idea(picks=["ITA"])]),
               episode(ep_id="a2", date="2026-11-23", ideas=[idea(picks=["ITA"])])]
        con = self.load(eps)
        mid = con.execute("SELECT mention_id FROM signals WHERE episode_id = 'a1' AND book = 'theme'").fetchone()[0]
        con.execute("INSERT INTO orders (client_order_id, mention_id, role, symbol, side, status) "
                    "VALUES ('a1-T-ITA', ?, 'entry', 'ITA', 'buy', 'filled')", (mid,))
        con.execute("UPDATE orders SET notional = 500 WHERE client_order_id = 'a1-T-ITA'")
        con.commit()
        plan = {r["episode"]: r for r in self.plan(con) if r["book"] == "theme"}
        self.assertEqual(plan["a1"]["action"], "EXISTS")
        self.assertEqual(plan["a2"]["action"], "SKIP")
        self.assertIn("already hold", plan["a2"]["note"])


if __name__ == "__main__":
    unittest.main()

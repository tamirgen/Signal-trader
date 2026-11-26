"""Tests for the signal filter and the next-open date logic.  Run: python -m unittest -v"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
from common import (add_trading_days, current_session, episode_exclusion, evaluate_mention,  # noqa: E402
                    horizon_bucket, load_rules, next_trading_day, normalize_ticker)
from ingest import classify_episode  # noqa: E402

D = dt.date.fromisoformat
RULES = load_rules()


def mention(**kw):
    m = {"asset": "X", "sentiment": "bullish", "confidence": "medium", "ticker_stated": "NVDA",
         "horizon": "medium", "speaker": "S"}
    m.update(kw)
    return m


class SignalFilter(unittest.TestCase):
    def test_bullish_medium_and_high_with_ticker_are_eligible(self):
        self.assertEqual(evaluate_mention(mention(), RULES), ("NVDA", None))
        self.assertEqual(evaluate_mention(mention(confidence="high"), RULES), ("NVDA", None))

    def test_bearish_is_skipped_with_reason(self):
        self.assertEqual(evaluate_mention(mention(sentiment="bearish"), RULES)[1], "sentiment bearish")

    def test_neutral_and_mixed_are_skipped(self):
        for s in ("neutral", "mixed"):
            self.assertIsNotNone(evaluate_mention(mention(sentiment=s), RULES)[1])

    def test_low_confidence_is_skipped(self):
        self.assertEqual(evaluate_mention(mention(confidence="low"), RULES)[1], "confidence low")

    def test_missing_stated_ticker_is_skipped_even_if_inferred(self):
        ticker, reason = evaluate_mention(mention(ticker_stated="", ticker_inferred="MU"), RULES)
        self.assertIsNone(ticker)
        self.assertEqual(reason, "no stated ticker")

    def test_company_name_instead_of_ticker_is_skipped(self):
        self.assertIn("not a symbol", evaluate_mention(mention(ticker_stated="Nvidia"), RULES)[1])

    def test_ticker_normalization(self):
        for raw in ("BRK.B", "brk.b", "BRK-B", "BRK/B", " $BRK.B "):
            self.assertEqual(normalize_ticker(raw), "BRK.B", raw)
        self.assertIsNone(normalize_ticker("AMD, MU"))
        self.assertIsNone(normalize_ticker(""))

    def test_table_four_2026_11_20_is_excluded(self):
        self.assertIsNotNone(episode_exclusion({"podcast": "Table Four", "date": "2026-11-20"}, RULES))
        self.assertIsNotNone(episode_exclusion({"podcast": "Table Four Weekly", "date": "2026-11-20"}, RULES))
        self.assertIsNone(episode_exclusion({"podcast": "Table Four", "date": "2026-11-21"}, RULES))
        self.assertIsNone(episode_exclusion({"podcast": "All-In", "date": "2026-11-20"}, RULES))

    def test_excluded_episode_mentions_are_all_skipped(self):
        ep = {"id": "t4", "podcast": "Table Four", "date": "2026-11-20", "mentions": [mention()]}
        [(_, _, _, reason)] = classify_episode(ep, RULES)
        self.assertTrue(reason.startswith("episode excluded"))

    def test_one_signal_per_ticker_per_episode_keeps_highest_confidence(self):
        ep = {"id": "e", "podcast": "P", "date": "2026-11-17",
              "mentions": [mention(confidence="medium"), mention(confidence="high"), mention(ticker_stated="MU")]}
        reasons = [r for _, _, _, r in classify_episode(ep, RULES)]
        self.assertEqual(reasons, ["duplicate ticker in episode", None, None])

    def test_horizon_buckets(self):
        self.assertEqual(horizon_bucket("short", RULES), "short")
        self.assertEqual(horizon_bucket("near term", RULES), "short")
        self.assertEqual(horizon_bucket("two to three quarters", RULES), "medium")
        self.assertEqual(horizon_bucket("2027-2028", RULES), "long")
        self.assertEqual(horizon_bucket("unknown", RULES), "medium")
        self.assertEqual(RULES["horizons"], {"short": 20, "medium": 60, "long": 180})


class NextOpen(unittest.TestCase):
    def setUp(self):
        common._alpaca_days = None  # use the built-in NYSE holiday list

    def test_weekday_goes_to_next_day(self):
        self.assertEqual(next_trading_day(D("2026-11-17")), D("2026-11-18"))  # Tue -> Wed

    def test_friday_and_weekend_go_to_monday(self):
        for d in ("2026-11-20", "2026-11-21", "2026-11-22"):
            self.assertEqual(next_trading_day(D(d)), D("2026-11-23"), d)

    def test_episode_on_sunday(self):
        self.assertEqual(next_trading_day(D("2026-11-01")), D("2026-11-02"))

    def test_skips_holidays(self):
        self.assertEqual(next_trading_day(D("2026-11-25")), D("2026-11-27"))  # Thanksgiving
        self.assertEqual(next_trading_day(D("2026-12-24")), D("2026-12-28"))  # Christmas + weekend
        self.assertEqual(next_trading_day(D("2026-12-31")), D("2027-01-04"))  # New Year + weekend

    def test_add_trading_days(self):
        self.assertEqual(add_trading_days(D("2026-11-23"), 5), D("2026-12-01"))  # skips Thanksgiving
        self.assertEqual(add_trading_days(D("2026-11-23"), 0), D("2026-11-23"))

    def test_current_session(self):
        et = common.ET
        self.assertEqual(current_session(dt.datetime(2026, 11, 23, 8, 0, tzinfo=et)), D("2026-11-23"))   # pre-open
        self.assertEqual(current_session(dt.datetime(2026, 11, 23, 15, 59, tzinfo=et)), D("2026-11-23"))
        self.assertEqual(current_session(dt.datetime(2026, 11, 23, 16, 0, tzinfo=et)), D("2026-11-24"))  # after close
        self.assertEqual(current_session(dt.datetime(2026, 11, 26, 10, 0, tzinfo=et)), D("2026-11-27"))  # holiday


if __name__ == "__main__":
    unittest.main()

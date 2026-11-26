"""The project must only ever trade Alpaca's paper account. Run: python -m unittest -v"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402


class PaperOnly(unittest.TestCase):
    def test_live_endpoint_is_refused(self):
        with mock.patch.dict(os.environ, {"ALPACA_KEY": "k", "ALPACA_SECRET": "s",
                                          "ALPACA_BASE_URL": "https://api.alpaca.markets"}):
            with self.assertRaises(SystemExit):
                common.trading_client()

    def test_any_other_endpoint_is_refused(self):
        with mock.patch.dict(os.environ, {"ALPACA_KEY": "k", "ALPACA_SECRET": "s",
                                          "ALPACA_BASE_URL": "https://example.com"}):
            with self.assertRaises(SystemExit):
                common.trading_client()

    def test_paper_endpoint_builds_a_paper_client(self):
        with mock.patch.dict(os.environ, {"ALPACA_KEY": "k", "ALPACA_SECRET": "s",
                                          "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/"}):
            client = common.trading_client()
        self.assertIn("paper-api.alpaca.markets", client._base_url)

    def test_non_paper_account_is_refused_before_any_order(self):
        tc = mock.Mock()
        tc.get_account.return_value = SimpleNamespace(account_number="123456789")
        with self.assertRaises(SystemExit):
            common.assert_paper(tc)

    def test_paper_account_passes(self):
        tc = mock.Mock()
        tc.get_account.return_value = SimpleNamespace(account_number="PA3ABCDEF123")
        common.assert_paper(tc)  # no exception

    def test_workflow_hardcodes_the_paper_url(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml").read_text()
        self.assertIn("ALPACA_BASE_URL: https://paper-api.alpaca.markets", text)
        self.assertNotIn("api.alpaca.markets/v2", text.replace("paper-api.alpaca.markets", ""))


if __name__ == "__main__":
    unittest.main()

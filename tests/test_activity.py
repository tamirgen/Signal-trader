"""A later run must not overwrite the day's earlier report. Run: python -m unittest -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import activity  # noqa: E402


class AppendReport(unittest.TestCase):
    def test_first_write_creates_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "2026-11-27.md"
            activity.append_report(p, "# Run 1\n")
            self.assertEqual(p.read_text(), "# Run 1\n")

    def test_second_run_keeps_the_first_runs_trades(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "2026-11-27.md"
            activity.append_report(p, "# Run 1\n### BUY NVDA\n")
            activity.append_report(p, "# Run 2\nNo new trades.\n")
            text = p.read_text()
            self.assertIn("BUY NVDA", text)
            self.assertIn("No new trades.", text)
            self.assertLess(text.index("Run 1"), text.index("Run 2"))


if __name__ == "__main__":
    unittest.main()

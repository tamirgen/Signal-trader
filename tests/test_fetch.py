"""Tests for episode discovery and transcript handling. No network: HTTP and Claude are faked.
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

import fetch  # noqa: E402

RSS = b"""<?xml version="1.0"?>
<rss xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>Table Four</title><description>Yonatan Adiri and Gili Beiman</description>
<item><title>New episode</title><guid>ep-2</guid><pubDate>Wed, 25 Nov 2026 10:00:00 GMT</pubDate>
  <enclosure url="https://x/ep2.mp3" type="audio/mpeg"/>
  <podcast:transcript url="https://x/ep2.vtt" type="text/vtt"/>
  <itunes:summary>Chips and oil</itunes:summary></item>
<item><title>Old episode</title><guid>ep-1</guid><pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate>
  <enclosure url="https://x/ep1.mp3" type="audio/mpeg"/></item>
</channel></rss>"""

YT = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
<title>All-In Podcast</title>
<entry><yt:videoId>abc123</yt:videoId><title>E200: Markets</title><published>2026-11-25T18:00:00+00:00</published>
  <media:group><media:description>Besties discuss</media:description></media:group></entry>
</feed>"""

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
<v Neil>Micron is cheap here.

2
00:00:04.000 --> 00:00:06.000
<v Neil>Micron is cheap here.
"""

TODAY = dt.date(2026, 11, 26)


class Parsing(unittest.TestCase):
    def test_parse_feed(self):
        items = fetch.parse_feed(RSS)
        self.assertEqual([i["guid"] for i in items], ["ep-2", "ep-1"])
        self.assertEqual(items[0]["transcript_url"], "https://x/ep2.vtt")
        self.assertEqual(items[0]["audio_url"], "https://x/ep2.mp3")
        self.assertEqual(items[0]["description"], "Chips and oil")
        self.assertIsNone(items[1]["transcript_url"])

    def test_parse_youtube_feed(self):
        title, items = fetch.parse_youtube_feed(YT)
        self.assertEqual(title, "All-In Podcast")
        self.assertEqual(items[0]["video_id"], "abc123")
        self.assertEqual(items[0]["link"], "https://www.youtube.com/watch?v=abc123")

    def test_vtt_to_text_keeps_speaker_and_drops_duplicates(self):
        self.assertEqual(fetch.captions_to_text(VTT, "text/vtt"), "Neil: Micron is cheap here.")

    def test_podcast_json_transcript(self):
        raw = json.dumps({"segments": [{"speaker": "A", "body": "Hi"}, {"speaker": "A", "body": "there"},
                                       {"speaker": "B", "body": "Yo"}]})
        self.assertEqual(fetch.captions_to_text(raw, "application/json"), "A: Hi there\nB: Yo")

    def test_select_new_respects_lookback_and_seen(self):
        items = fetch.parse_feed(RSS)
        self.assertEqual([i["guid"] for i in fetch.select_new(items, set(), 4, 5, TODAY)], ["ep-2"])
        self.assertEqual(fetch.select_new(items, {"ep-2"}, 4, 5, TODAY), [])
        self.assertEqual(len(fetch.select_new(items, set(), 30, 5, TODAY)), 2)

    def test_inbox_front_matter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nvda.txt"
            p.write_text("podcast: NVIDIA call\ndate: 2027-02-25\n---\nRevenue grew.")
            meta, text = fetch.read_inbox_file(p)
            self.assertEqual(meta, {"podcast": "NVIDIA call", "date": "2027-02-25"})
            self.assertEqual(text, "Revenue grew.")
            p.write_text("No front matter here.\nJust text.")
            self.assertEqual(fetch.read_inbox_file(p), ({}, "No front matter here.\nJust text."))

    def test_episode_id_is_stable(self):
        a = fetch.episode_id("Table Four", "2026-11-25", "ep-2")
        self.assertEqual(a, fetch.episode_id("Table Four", "2026-11-25", "ep-2"))
        self.assertTrue(a.startswith("table-four-2026-11-25-"))


class EndToEnd(unittest.TestCase):
    """A full fetch run against fake HTTP and a fake Claude."""

    def test_run_appends_signals_and_marks_seen(self):
        long_vtt = VTT + "\n".join(f"\n{i}\n00:01:{i:02d}.000 --> 00:01:{i:02d}.500\nline {i} " + "x" * 400
                                   for i in range(10, 59))
        pages = {"https://x/feed.xml": RSS, "https://x/ep2.vtt": long_vtt.encode()}
        extracted = {"summary": "s", "themes": [], "catalysts": [], "mentions": [
            {"asset": "Micron", "sentiment": "bullish", "speaker": "Neil", "confidence": "high", "horizon": "medium",
             "claim_type": "opinion", "kind": "stock", "ticker_stated": "MU", "ticker_inferred": "",
             "catalyst_date": "", "reasoning": "cheap", "quote": "Micron is cheap here.", "timestamp": ""}]}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "sources.yaml").write_text(
                "settings: {lookback_days: 4, max_episodes_per_source: 2, min_transcript_chars: 1000, "
                "max_transcript_chars: 600000}\nsources:\n"
                "  - {name: Table Four, type: podcast, source: Podcasts, feed_url: 'https://x/feed.xml'}\n")
            (d / "signals.json").write_text("[]")
            fake_extract = mock.Mock(return_value=extracted)
            with mock.patch.multiple(fetch, ROOT=d, SIGNALS=d / "signals.json", STATE=d / "state" / "sources.json",
                                     INBOX=d / "inbox", REPORTS=d / "reports", LOG=d / "reports" / "fetch_log.json",
                                     http_get=lambda url, timeout=60: pages[url]), \
                 mock.patch("extract.extract", fake_extract), \
                 mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                 mock.patch("fetch.dt") as fdt:
                fdt.datetime.now.return_value = dt.datetime(2026, 11, 26, 7, 0)
                fdt.timedelta, fdt.date = dt.timedelta, dt.date
                log = fetch.run(dry=False)
                again = fetch.run(dry=False)  # second run: nothing new
            signals = json.loads((d / "signals.json").read_text())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["podcast"], "Table Four")
        self.assertEqual(signals[0]["date"], "2026-11-25")
        self.assertEqual(signals[0]["transcript_source"], "feed transcript")
        self.assertEqual(signals[0]["mentions"][0]["ticker_stated"], "MU")
        self.assertEqual(log["episodes"][0]["status"], "extracted")
        self.assertEqual(fake_extract.call_count, 1)
        self.assertEqual(again["episodes"], [])


if __name__ == "__main__":
    unittest.main()

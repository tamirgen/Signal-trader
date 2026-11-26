"""Load signals.json into SQLite (episodes, signals, orders) and apply the signal filter.
Every mention is stored: status 'eligible', or 'skipped' with the reason.

    python ingest.py [--file signals.json]
"""
from __future__ import annotations

import argparse
import json

from common import ROOT, db, episode_exclusion, evaluate_mention, horizon_bucket, load_rules

CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def classify_episode(ep: dict, rules: dict) -> list[tuple[int, dict, str | None, str | None]]:
    """[(index, mention, ticker, skip_reason)]; dedupes eligible mentions by ticker within the episode,
    keeping the highest confidence (then the first)."""
    excluded = episode_exclusion(ep, rules)
    out = []
    for i, m in enumerate(ep.get("mentions") or []):
        ticker, reason = evaluate_mention(m, rules)
        out.append((i, m, ticker, f"episode excluded: {excluded}" if excluded else reason))
    best: dict[str, int] = {}
    for i, m, ticker, reason in out:
        if reason is None and (ticker not in best or
                               CONF_RANK[m["confidence"].lower()] > CONF_RANK[out[best[ticker]][1]["confidence"].lower()]):
            best[ticker] = i
    return [(i, m, t, "duplicate ticker in episode" if r is None and best[t] != i else r)
            for i, m, t, r in out]


def ingest(path, rules) -> dict:
    con = db()
    counts = {"episodes": 0, "mentions": 0, "eligible": 0}
    for ep in json.load(open(path)):
        counts["episodes"] += 1
        con.execute(
            """INSERT INTO episodes (id, podcast, source, date, summary) VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET podcast=excluded.podcast, source=excluded.source,
               date=excluded.date, summary=excluded.summary""",
            (ep["id"], ep.get("podcast"), ep.get("source") or "unknown", ep["date"], ep.get("summary")))
        for i, m, ticker, reason in classify_episode(ep, rules):
            mid = f"{ep['id']}#{i}"
            bucket = horizon_bucket(m.get("horizon"), rules)
            # Demote first so the one-per-ticker unique index can't trip while re-ingesting.
            con.execute("UPDATE signals SET status = 'skipped' WHERE mention_id = ?", (mid,))
            con.execute(
                """INSERT INTO signals (mention_id, episode_id, asset, sentiment, speaker, confidence, horizon,
                       claim_type, ticker_stated, ticker_inferred, catalyst_date, ticker, horizon_bucket,
                       hold_days, status, skip_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(mention_id) DO UPDATE SET asset=excluded.asset, sentiment=excluded.sentiment,
                       speaker=excluded.speaker, confidence=excluded.confidence, horizon=excluded.horizon,
                       claim_type=excluded.claim_type, ticker_stated=excluded.ticker_stated,
                       ticker_inferred=excluded.ticker_inferred, catalyst_date=excluded.catalyst_date,
                       ticker=excluded.ticker, horizon_bucket=excluded.horizon_bucket,
                       hold_days=excluded.hold_days, status=excluded.status, skip_reason=excluded.skip_reason""",
                (mid, ep["id"], m.get("asset"), m.get("sentiment"), m.get("speaker") or "unknown",
                 m.get("confidence"), m.get("horizon"), m.get("claim_type"), m.get("ticker_stated"),
                 m.get("ticker_inferred"), m.get("catalyst_date"), ticker, bucket,
                 rules["horizons"][bucket], "skipped" if reason else "eligible", reason))
            counts["mentions"] += 1
            counts["eligible"] += reason is None
    con.commit()
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=ROOT / "signals.json")
    c = ingest(ap.parse_args().file, load_rules())
    print(f"ingested {c['episodes']} episodes, {c['mentions']} mentions: "
          f"{c['eligible']} eligible, {c['mentions'] - c['eligible']} skipped")

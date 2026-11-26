"""Check every source for new episodes, get transcripts, extract signals with Claude,
and append them to signals.json. Writes reports/fetch_log.json for the daily activity report.

    python fetch.py              # check all sources (sources.yaml) and inbox/
    python fetch.py --dry-run    # find new episodes and transcripts, but don't call Claude or write signals.json

Needs ANTHROPIC_API_KEY. Optional: DEEPGRAM_API_KEY transcribes podcast audio when a feed has no
transcript of its own; YOUTUBE_PROXY_URL routes YouTube caption requests through a proxy if
YouTube blocks the runner.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import io
import json
import os
import re
import shutil
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from common import ET as TZ_ET
from common import REPORTS, ROOT, load_env

SIGNALS = ROOT / "signals.json"
STATE = ROOT / "state" / "sources.json"
INBOX = ROOT / "inbox"
LOG = REPORTS / "fetch_log.json"
UA = "Mozilla/5.0 (compatible; signal-trader/1.0)"
NS = {"podcast": "https://podcastindex.org/namespace/1.0",
      "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
      "content": "http://purl.org/rss/1.0/modules/content/"}


# ---------- http ----------
def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- helpers ----------
def short(exc: Exception) -> str:
    """One-line error for reports (some libraries raise multi-paragraph messages)."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    msg = lines[0] if lines else ""
    if "blocking requests from your IP" in str(exc):
        msg = "YouTube blocked the request (cloud IP); set YOUTUBE_PROXY_URL or use the podcast feed"
    return f"{type(exc).__name__}: {msg}"[:240]

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def episode_id(podcast: str, date: str, guid: str) -> str:
    return f"{slug(podcast)}-{date}-{hashlib.sha1(guid.encode()).hexdigest()[:4]}"


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def captions_to_text(raw: str, kind: str) -> str:
    """Plain text from a VTT/SRT/podcast-JSON/HTML transcript."""
    kind = (kind or "").lower()
    if "json" in kind:
        segs = json.loads(raw).get("segments", [])
        out, last = [], None
        for s in segs:
            spk = s.get("speaker")
            prefix = f"\n{spk}: " if spk and spk != last else " "
            last = spk or last
            out.append(prefix + (s.get("body") or "").strip())
        return "".join(out).strip()
    if "vtt" in kind or "srt" in kind or raw.lstrip().startswith("WEBVTT"):
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if (not line or line == "WEBVTT" or line.isdigit() or "-->" in line
                    or line.startswith(("NOTE", "STYLE", "Kind:", "Language:"))):
                continue
            line = re.sub(r"<v\s+([^>]+)>", r"\1: ", line)
            line = re.sub(r"<[^>]+>", "", line)
            if not lines or lines[-1] != line:
                lines.append(line)
        return "\n".join(lines)
    if "html" in kind:
        return strip_html(raw)
    return raw.strip()


# ---------- podcast feeds ----------
def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        guid = (it.findtext("guid") or it.findtext("link") or it.findtext("title") or "").strip()
        pub = it.findtext("pubDate")
        try:
            published = email.utils.parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        enc = it.find("enclosure")
        tr = it.find("podcast:transcript", NS)
        items.append({
            "guid": guid,
            "title": (it.findtext("title") or "").strip(),
            "published": published,
            "audio_url": enc.get("url") if enc is not None else None,
            "transcript_url": tr.get("url") if tr is not None else None,
            "transcript_type": tr.get("type") if tr is not None else None,
            "link": (it.findtext("link") or "").strip(),
            "description": strip_html(it.findtext("itunes:summary", namespaces=NS)
                                      or it.findtext("description") or "")[:2000],
        })
    return items


def resolve_feed(src: dict, state: dict) -> str | None:
    if src.get("feed_url"):
        return src["feed_url"]
    if state.get("feed_url"):
        return state["feed_url"]
    q = urllib.parse.urlencode({"term": src["search"], "media": "podcast", "entity": "podcast", "limit": 10})
    results = json.loads(http_get(f"https://itunes.apple.com/search?{q}")).get("results", [])
    keywords = [k.lower() for k in src.get("match", [])]
    for r in results:
        url = r.get("feedUrl")
        if not url:
            continue
        try:
            text = http_get(url, timeout=30)[:500_000].decode("utf-8", "ignore").lower()
        except Exception:
            continue
        if all(k in text for k in keywords):
            state["feed_url"] = url
            state["feed_title"] = r.get("collectionName")
            return url
    return None


def deepgram_transcribe(audio_url: str) -> str:
    q = urllib.parse.urlencode({"model": "nova-3", "smart_format": "true", "diarize": "true", "paragraphs": "true"})
    req = urllib.request.Request(
        f"https://api.deepgram.com/v1/listen?{q}", data=json.dumps({"url": audio_url}).encode(),
        headers={"Authorization": f"Token {os.environ['DEEPGRAM_API_KEY'].strip()}",
                 "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=1200) as r:
        alt = json.loads(r.read())["results"]["channels"][0]["alternatives"][0]
    return (alt.get("paragraphs") or {}).get("transcript") or alt.get("transcript", "")


def podcast_transcript(item: dict) -> tuple[str | None, str]:
    """(text, how) - how names the transcript source, or why there is none."""
    if item["transcript_url"]:
        raw = http_get(item["transcript_url"]).decode("utf-8", "ignore")
        return captions_to_text(raw, item["transcript_type"] or item["transcript_url"]), "feed transcript"
    if not item["audio_url"]:
        return None, "no audio or transcript in feed"
    if not os.environ.get("DEEPGRAM_API_KEY", "").strip():
        return None, "feed has no transcript and DEEPGRAM_API_KEY is not set"
    return deepgram_transcribe(item["audio_url"]), "Deepgram speech-to-text"


# ---------- YouTube ----------
ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/"}


def resolve_channel(src: dict, state: dict) -> str | None:
    """Channel id (UC...) from config, cache, or the channel's @handle page."""
    cid = src.get("channel_id") or state.get("channel_id")
    if cid:
        return cid
    page = http_get(f"https://www.youtube.com/{src['handle']}").decode("utf-8", "ignore")
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', page) or \
        re.search(r'youtube\.com/channel/(UC[\w-]{22})', page)
    if m:
        state["channel_id"] = m.group(1)
        return m.group(1)
    return None


def parse_youtube_feed(xml_bytes: bytes) -> tuple[str, list[dict]]:
    root = ET.fromstring(xml_bytes)
    title = root.findtext("a:title", namespaces=ATOM) or ""
    items = []
    for e in root.findall("a:entry", ATOM):
        vid = e.findtext("yt:videoId", namespaces=ATOM)
        pub = e.findtext("a:published", namespaces=ATOM)
        items.append({
            "guid": f"yt-{vid}", "video_id": vid,
            "title": (e.findtext("a:title", namespaces=ATOM) or "").strip(),
            "published": dt.datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None,
            "link": f"https://www.youtube.com/watch?v={vid}",
            "description": (e.findtext("media:group/media:description", namespaces=ATOM) or "")[:2000],
        })
    return title, items


def youtube_transcript(video_id: str) -> tuple[str | None, str]:
    """Captions text with a [mm:ss] marker about once a minute. Set YOUTUBE_PROXY_URL if YouTube
    blocks the server's IP (common on cloud runners)."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig
    proxy = os.environ.get("YOUTUBE_PROXY_URL", "").strip()
    api = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None)
    snippets = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    out, next_mark = [], 0.0
    for sn in snippets:
        if sn.start >= next_mark:
            out.append(f"\n[{int(sn.start // 60)}:{int(sn.start % 60):02d}] ")
            next_mark = sn.start + 60
        out.append(sn.text.replace("\n", " ") + " ")
    return "".join(out).strip(), "YouTube captions"


# ---------- FOMC ----------
FED = "https://www.federalreserve.gov"


def fomc_items() -> list[dict]:
    page = http_get(f"{FED}/monetarypolicy/fomccalendars.htm").decode("utf-8", "ignore")
    dates = sorted(set(re.findall(r"fomcpresconf(\d{8})\.htm", page)))
    return [{"guid": f"fomc-{d}", "title": f"FOMC press conference {d[:4]}-{d[4:6]}-{d[6:]}",
             "published": dt.datetime.strptime(d, "%Y%m%d").replace(tzinfo=TZ_ET),
             "pdf": f"{FED}/mediacenter/files/FOMCpresconf{d}.pdf",
             "link": f"{FED}/monetarypolicy/fomcpresconf{d}.htm", "description": ""} for d in dates]


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


# ---------- inbox ----------
def read_inbox_file(path: Path) -> tuple[dict, str]:
    """Front matter (optional) is 'key: value' lines at the top, ended by a line '---'."""
    text = pdf_text(path.read_bytes()) if path.suffix.lower() == ".pdf" else path.read_text("utf-8", "ignore")
    meta: dict = {}
    head, sep, body = text.partition("\n---\n")
    if sep and all(re.match(r"^\s*\w+\s*:", ln) for ln in head.strip().splitlines() if ln.strip()):
        for ln in head.strip().splitlines():
            k, _, v = ln.partition(":")
            meta[k.strip().lower()] = v.strip()
        text = body
    return meta, text


# ---------- main ----------
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def select_new(items: list[dict], seen: set, lookback_days: int, limit: int, today: dt.date) -> list[dict]:
    cutoff = today - dt.timedelta(days=lookback_days)
    fresh = [i for i in items if i["published"] and i["published"].astimezone(TZ_ET).date() >= cutoff
             and i["guid"] not in seen]
    return sorted(fresh, key=lambda i: i["published"])[:limit]


def build_episode(podcast, source, date, title, link, how, extracted) -> dict:
    return {"id": None, "podcast": podcast, "source": source, "date": date, "title": title, "url": link,
            "transcript_source": how, "savedAt": int(time.time() * 1000), **extracted}


def run(dry: bool) -> dict:
    load_env()
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())
    st = cfg["settings"]
    state = load_json(STATE, {})
    signals = load_json(SIGNALS, [])
    known_ids = {e["id"] for e in signals}
    today = dt.datetime.now(TZ_ET).date()
    log = {"date": today.isoformat(), "dry_run": dry, "sources": [], "episodes": [], "errors": []}

    if not dry and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        log["errors"].append("ANTHROPIC_API_KEY is not set: new episodes were found but not processed.")
        dry = True

    def process(podcast, source, item, get_text, s_state):
        date = item["published"].astimezone(TZ_ET).date().isoformat()
        ep = {"podcast": podcast, "title": item["title"], "date": date, "link": item.get("link")}
        try:
            text, how = get_text()
            ep["transcript_source"] = how
            if not text:
                ep["status"] = f"skipped: {how}"
                if "is not set" not in how:  # a missing key is fixable, so keep retrying; other reasons are permanent
                    s_state.setdefault("seen", []).append(item["guid"])
                return ep
            ep["transcript_chars"] = len(text)
            if len(text) < st.get("min_transcript_chars", 0):
                ep["status"] = f"skipped: too short ({len(text):,} chars), likely a clip"
                s_state.setdefault("seen", []).append(item["guid"])
                return ep
            if len(text) > st["max_transcript_chars"]:
                ep["status"] = f"skipped: transcript too long ({len(text):,} chars)"
                s_state.setdefault("seen", []).append(item["guid"])
                return ep
            if dry:
                ep["status"] = "found (dry run, not extracted)"
                return ep
            from extract import extract
            data = extract(podcast, item["title"], date, item.get("description", ""), text)
            e = build_episode(podcast, source, date, item["title"], item.get("link"), how, data)
            e["id"] = episode_id(podcast, date, item["guid"])
            if e["id"] not in known_ids:
                signals.append(e)
                known_ids.add(e["id"])
            s_state.setdefault("seen", []).append(item["guid"])
            ep.update(status="extracted", id=e["id"], mentions=len(data["mentions"]))
        except Exception as exc:  # transient: leave unseen so tomorrow retries
            ep["status"] = f"error: {short(exc)}"
            log["errors"].append(f"{podcast} / {item['title']}: {short(exc)}")
            traceback.print_exc()
        return ep

    for src in cfg["sources"]:
        s_state = state.setdefault(src["name"], {})
        entry = {"name": src["name"], "type": src["type"]}
        try:
            if src["type"] == "podcast":
                url = resolve_feed(src, s_state)
                if not url:
                    entry["status"] = "feed not found: set feed_url in sources.yaml"
                    log["sources"].append(entry)
                    continue
                entry["feed"] = s_state.get("feed_title") or url
                items = parse_feed(http_get(url))
                getter = lambda it: (lambda: podcast_transcript(it))  # noqa: E731
            elif src["type"] == "youtube":
                cid = resolve_channel(src, s_state)
                if not cid:
                    entry["status"] = "channel not found: set channel_id in sources.yaml"
                    log["sources"].append(entry)
                    continue
                ch_title, items = parse_youtube_feed(http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
                if not all(k.lower() in ch_title.lower() for k in src.get("match", [])):
                    s_state.pop("channel_id", None)
                    entry["status"] = f"channel '{ch_title}' doesn't match {src.get('match')}: set channel_id"
                    log["sources"].append(entry)
                    continue
                entry["feed"] = ch_title
                getter = lambda it: (lambda: youtube_transcript(it["video_id"]))  # noqa: E731
            elif src["type"] == "fomc":
                items = fomc_items()
                getter = lambda it: (lambda: (pdf_text(http_get(it["pdf"])), "Fed transcript PDF"))  # noqa: E731
            else:
                raise ValueError(f"unknown source type {src['type']}")
            new = select_new(items, set(s_state.get("seen", [])), st["lookback_days"], 50, today)
            entry.update(status="ok", latest=max((i["published"] for i in items if i["published"]), default=None),
                         new=len(new))
            if entry["latest"]:
                entry["latest"] = entry["latest"].astimezone(TZ_ET).date().isoformat()
            done = 0
            for it in new:  # the per-source cap counts only episodes sent to Claude, not skipped clips
                if done >= st["max_episodes_per_source"]:
                    break
                ep = process(src["name"], src["source"], it, getter(it), s_state)
                done += ep["status"] in ("extracted", "found (dry run, not extracted)") or ep["status"].startswith("error")
                if not ep["status"].startswith("skipped: too short"):
                    log["episodes"].append(ep)
        except Exception as exc:
            entry["status"] = f"error: {short(exc)}"
            log["errors"].append(f"{src['name']}: {short(exc)}")
            traceback.print_exc()
        s_state["seen"] = s_state.get("seen", [])[-300:]
        log["sources"].append(entry)

    # inbox: manually uploaded transcripts (earnings calls, filings, anything else)
    for path in sorted(p for p in INBOX.glob("*") if p.is_file() and p.suffix.lower() in (".txt", ".pdf")):
        meta, _ = read_inbox_file(path)
        podcast = meta.get("podcast") or path.stem
        date = meta.get("date") or today.isoformat()
        item = {"guid": f"inbox-{path.name}", "title": meta.get("title") or path.stem, "link": meta.get("url"),
                "published": dt.datetime.fromisoformat(date).replace(tzinfo=TZ_ET), "description": ""}
        ep = process(podcast, meta.get("source") or "Uploaded transcripts", item,
                     lambda p=path: (read_inbox_file(p)[1], "uploaded file"), state.setdefault("inbox", {}))
        log["episodes"].append(ep)
        if not dry and ep["status"] == "extracted":
            (INBOX / "processed").mkdir(exist_ok=True)
            shutil.move(str(path), INBOX / "processed" / path.name)

    if not dry:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, default=str))
        SIGNALS.write_text(json.dumps(signals, indent=1, ensure_ascii=False))
    REPORTS.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, indent=1, default=str))
    return log


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="don't call Claude or write signals.json")
    log = run(ap.parse_args().dry_run)
    for s in log["sources"]:
        print(f"  {s['name']:<24} {s['status']}  latest {s.get('latest', '-')}  new {s.get('new', 0)}")
    for e in log["episodes"]:
        print(f"  + {e['podcast']}: {e['title']} ({e['date']}) -> {e['status']}")
    for err in log["errors"]:
        print(f"  ! {err}")


if __name__ == "__main__":
    main()

"""Turn the world events discussed in each episode into stock ideas ("theme logic").

For every episode in signals.json that has no `theme_ideas` yet, Claude reads the episode's summary,
themes, catalysts and extracted views, decides which world events matter for markets, and for each
one picks the matching theme from universe.yaml and the instruments in it that should benefit.
Results are stored back on the episode (signals.json stays the single source of truth).

    python themes.py              # process episodes that have no theme_ideas yet
    python themes.py --redo       # recompute all episodes
    python themes.py --dry-run    # show what would be processed; no API calls
"""
from __future__ import annotations

import argparse
import json
import os
from typing import List, Literal

import yaml
from pydantic import BaseModel, Field

from common import ROOT, episode_exclusion, load_env, load_rules

SIGNALS = ROOT / "signals.json"
UNIVERSE = ROOT / "universe.yaml"
MODEL = "claude-opus-5-5"


class Idea(BaseModel):
    theme: str = Field(description="Key of ONE theme from the universe list that this event affects.")
    event: str = Field(description="The world event or trend, in one plain sentence (what is happening).")
    effect: Literal["tailwind", "headwind"] = Field(
        description="tailwind = the event helps this theme's stocks; headwind = it hurts them.")
    strength: Literal["low", "medium", "high"] = Field(
        description="How directly and credibly the episode supports this effect. high = speakers state it "
                    "clearly with specifics; low = passing or speculative mention.")
    horizon: Literal["short", "medium", "long"] = Field(
        description="How long the effect should play out: short ~1 month, medium ~3 months, long ~9 months.")
    causal_chain: str = Field(description="2-3 short steps from the event to the stocks, e.g. 'A -> B -> C'.")
    invalidation: str = Field(description="What would show this idea is wrong.")
    picks: List[str] = Field(description="Up to 2 tickers from THIS theme's list that benefit most "
                                         "(prefer 1 ETF + 1 stock). Empty for headwind ideas.")
    speakers: str = Field(description="Who in the episode raised the underlying event, if known.")


class EpisodeIdeas(BaseModel):
    ideas: List[Idea]


SYSTEM = """You turn the world events discussed in finance podcasts and event transcripts into stock-market ideas.

You are given an episode (summary, themes, catalysts, extracted views) and a fixed UNIVERSE of themes, each with the
kind of event that helps it and the ETFs/stocks that capture it. For each genuinely market-relevant world event
(geopolitics, war, energy, rates, policy, technology build-out, supply chains, macro data):
- Pick the single best matching theme KEY from the universe. If the event hurts a theme, you may log it as a
  headwind; but prefer to look for the theme that BENEFITS (e.g. falling oil helps airlines_transport).
- Only tailwind ideas are traded. For those, choose up to 2 picks from that theme's own lists: prefer one ETF plus
  the single most direct stock. Never invent tickers or use ones outside the theme's list.
- Write the cause-and-effect chain concisely and state what would prove the idea wrong.
- Be selective: 0-5 ideas per episode. Skip events with no clear market link, and skip anything already
  obviously priced in or purely backward-looking. Return an empty list if nothing qualifies.
- Judge strength honestly. Use high only when the episode states the event firmly and the link to the theme is direct.
Company-specific earnings views are handled elsewhere; focus on events and trends that move a theme."""


def load_universe(path=UNIVERSE) -> dict:
    return yaml.safe_load(open(path))["themes"]


def universe_text(universe: dict) -> str:
    out = []
    for key, t in universe.items():
        out.append(f"- {key}: helped by: {t['description']}  ETFs: {', '.join(t.get('etfs') or []) or '-'}"
                   f"  Stocks: {', '.join(t.get('stocks') or []) or '-'}")
    return "\n".join(out)


def validate_idea(idea: dict, universe: dict, max_picks: int = 2) -> dict | None:
    """Keep only picks that belong to the idea's theme; ETFs first. None if the theme is unknown."""
    theme = universe.get(idea.get("theme"))
    if theme is None:
        return None
    etfs, stocks = list(theme.get("etfs") or []), list(theme.get("stocks") or [])
    wanted = [str(t).strip().upper() for t in idea.get("picks") or []]
    picks, seen = [], set()
    for t in [x for x in wanted if x in etfs] + [x for x in wanted if x in stocks]:
        if t not in seen:
            seen.add(t)
            picks.append(t)
    out = dict(idea)
    out["picks"] = picks[:max_picks] if idea.get("effect") == "tailwind" else []
    return out


def episode_brief(ep: dict) -> str:
    lines = [f"Podcast/source: {ep.get('podcast')}", f"Title: {ep.get('title') or ''}", f"Date: {ep.get('date')}",
             f"Summary: {ep.get('summary') or ''}", "", "Themes discussed:"]
    lines += [f"- {t.get('theme')}: {t.get('market_impact')}" for t in ep.get("themes") or []]
    lines += ["", "Catalysts:"] + [f"- {c.get('date')}: {c.get('event')} ({c.get('related_assets')})"
                                   for c in ep.get("catalysts") or []]
    lines += ["", "Views extracted from the episode:"]
    for m in ep.get("mentions") or []:
        lines.append(f"- [{m.get('speaker')}] {m.get('asset')} ({m.get('kind')}): {m.get('sentiment')}, "
                     f"{m.get('confidence')}. {m.get('reasoning')}")
    return "\n".join(lines)


def generate(ep: dict, universe: dict, rules: dict, client=None) -> list[dict]:
    import anthropic
    client = client or anthropic.Anthropic()
    user = (f"UNIVERSE (theme keys and their instruments):\n{universe_text(universe)}\n\n"
            f"<episode>\n{episode_brief(ep)}\n</episode>")
    resp = client.beta.messages.parse(
        model=os.environ.get("SIGNAL_MODEL", MODEL),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "high"},
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=EpisodeIdeas,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(resp.stop_details, 'category', None)}")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise RuntimeError(f"no complete output (stop_reason={resp.stop_reason})")
    max_picks = rules.get("thematic", {}).get("max_picks_per_idea", 2)
    ideas = [validate_idea(i.model_dump(), universe, max_picks) for i in resp.parsed_output.ideas]
    return [i for i in ideas if i]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redo", action="store_true", help="recompute every episode")
    ap.add_argument("--dry-run", action="store_true", help="list episodes to process; no API calls")
    args = ap.parse_args()
    load_env()
    rules, universe = load_rules(), load_universe()
    episodes = json.loads(SIGNALS.read_text())
    todo = [e for e in episodes if (args.redo or "theme_ideas" not in e) and not episode_exclusion(e, rules)]
    skipped = [e["id"] for e in episodes if episode_exclusion(e, rules)]
    print(f"{len(todo)} episode(s) to process" + (f"; {len(skipped)} excluded by rules" if skipped else ""))
    if args.dry_run or not todo:
        return
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("ANTHROPIC_API_KEY is not set.")
    failures = 0
    for ep in todo:
        try:
            ep["theme_ideas"] = generate(ep, universe, rules)
            tw = [i for i in ep["theme_ideas"] if i["effect"] == "tailwind"]
            print(f"  {ep['id']}: {len(ep['theme_ideas'])} idea(s), {len(tw)} tailwind")
        except Exception as exc:  # leave unprocessed; tomorrow retries
            failures += 1
            print(f"  ! {ep['id']}: {type(exc).__name__}: {str(exc)[:200]}")
    SIGNALS.write_text(json.dumps(episodes, indent=1, ensure_ascii=False))
    if failures:
        print(f"{failures} episode(s) failed and will be retried on the next run.")


if __name__ == "__main__":
    main()

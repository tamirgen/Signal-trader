"""Turn a transcript into signals.json fields with the Claude API (structured output)."""
from __future__ import annotations

import os
from typing import List, Literal

from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"


class Mention(BaseModel):
    asset: str = Field(description="What the view is about, e.g. 'NVIDIA', 'Long-duration bonds', 'Neoclouds'.")
    sentiment: Literal["bullish", "bearish", "neutral", "mixed"]
    speaker: str = Field(description="Name of the person holding the view; 'unclear' if it can't be told.")
    confidence: Literal["low", "medium", "high"] = Field(
        description="How strongly and specifically the speaker commits to the view.")
    horizon: str = Field(description="Time frame as said or implied: 'short', 'medium', 'long', or the speaker's words.")
    claim_type: Literal["opinion", "prediction", "reported_fact"]
    kind: str = Field(description="stock, sector, commodity, macro, asset class, index, private company, other")
    ticker_stated: str = Field(description=(
        "Exchange ticker (e.g. NVDA, BRK.B, SPY) ONLY when the speaker explicitly names one specific "
        "US-listed company or ETF. Empty for sectors, baskets, private companies, or anything inferred."))
    ticker_inferred: str = Field(description="Your best-guess tickers when not explicitly named, comma-separated; else empty.")
    catalyst_date: str = Field(description="Date or event the view hinges on, if any; else empty.")
    reasoning: str = Field(description="One or two sentences: why the speaker holds this view.")
    quote: str = Field(description="Short verbatim quote supporting the view, if available; else empty.")
    timestamp: str = Field(description="Timestamp in the transcript, if available; else empty.")


class Theme(BaseModel):
    theme: str
    market_impact: str


class Catalyst(BaseModel):
    date: str
    event: str
    related_assets: str


class EpisodeSignals(BaseModel):
    summary: str = Field(description="3-4 sentence summary of the episode's market-relevant content.")
    themes: List[Theme]
    catalysts: List[Catalyst]
    mentions: List[Mention]


SYSTEM = """You extract investing signals from finance podcast and event transcripts.

Return every distinct market view a speaker expresses about an asset, sector, commodity, rate, or macro variable.
These signals drive automated paper trades, so precision matters more than coverage:
- ticker_stated must be a real, currently listed US ticker of the ONE company or ETF the speaker explicitly named.
  Never fill it for a sector, a list of companies, a private company (e.g. SpaceX, OpenAI, Anthropic), or a guess.
- sentiment is the speaker's view, not yours. A neutral fact report is "neutral" unless the speaker draws a direction.
- confidence: high = explicit conviction or a stated position/pick; medium = clear directional view;
  low = passing mention or heavily hedged.
- Attribute views to the person who holds them. Use names from the transcript; use "unclear" when unsure.
- Do not invent numbers or quotes. Quotes must be verbatim from the transcript.
- One mention per distinct view; merge repetitions of the same view."""


def extract(podcast: str, title: str, date: str, description: str, transcript: str, client=None) -> dict:
    """Returns {"summary", "themes", "catalysts", "mentions"} as plain dicts. Raises on refusal/failure."""
    import anthropic
    client = client or anthropic.Anthropic()
    user = (f"Podcast/source: {podcast}\nEpisode title: {title}\nDate: {date}\n"
            f"Episode description: {description or '(none)'}\n\n<transcript>\n{transcript}\n</transcript>")
    resp = client.beta.messages.parse(
        model=os.environ.get("SIGNAL_MODEL", MODEL),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                      # if the model declines, the API retries on a fallback model
        output_config={"effort": "high"},
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=EpisodeSignals,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(resp.stop_details, 'category', None)}")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise RuntimeError(f"no complete output (stop_reason={resp.stop_reason})")
    return resp.parsed_output.model_dump()

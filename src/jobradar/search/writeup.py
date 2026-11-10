"""Detailed, application-ready rationale for the few postings that actually
made the cut (ranking.select_matches() output) — the "few, high-quality"
half of the cheap-bulk-pass / expensive-finalist-pass split. Uses Sonnet
since the quality of the explanation matters more here than at the bulk
scoring stage.
"""

from __future__ import annotations

import logging
import re

import anthropic

from .. import usage
from ..llm import MODEL_DEFAULTS, model_for
from ..models import FinalPosting
from .ranking import RankedPosting

logger = logging.getLogger(__name__)

# Overridable per run via JOBRADAR_WRITEUP_MODEL; see jobradar.llm.
DEFAULT_WRITEUP_MODEL = MODEL_DEFAULTS["JOBRADAR_WRITEUP_MODEL"]


_WRITEUP_PROMPT = """\
Write an application-ready rationale for why this job posting is a {tier} match \
for the candidate, using their CV "{best_cv}" and their stated career identity.

Cover, in a few short paragraphs:
- Why it fits (skills + interest), referencing specifics from the posting.
- What to highlight when applying (most relevant experience/achievements).
- Any concerns or gaps worth being aware of going in.

Keep it concise and concrete — no generic filler. Reply with the write-up \
itself in Markdown: no preamble, no closing remark, and no # headings (the \
report already heads it with the posting's title); use a bold lead-in for each \
part instead.

Job posting:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Scoring notes from the initial pass: {brief_reason}
"""


# One retry, for the same two reasons scoring has one. A transient API error
# should not cost a finalist its write-up. And a response that runs past the
# ceiling is usually an outlier, not the norm: the three write-ups on
# 2026-09-23 ran about 650 tokens each, while the Julius Baer one on 2026-09-22
# ran past 2048 and was cut mid-sentence. Raising the ceiling was already tried
# once (it was 1024) and only moved the cut; a second draw usually lands.
_WRITEUP_ATTEMPTS = 2

# A Markdown heading line. The report gives each match a "## [TIER] ..." heading
# and apply/findings.py ends a match at the next "## " line, so a heading inside
# a write-up would cut the write-up off for the apply pipeline.
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.M)


def _demote_headings(text: str) -> str:
    return _HEADING_RE.sub(r"**\1**", text)

# Appended when every attempt ran out of room. The report is the only place the
# reader looks — the run log already warned about truncation and it went
# unnoticed until a sentence visibly failed to end — so it has to be said there.
_TRUNCATION_NOTE = (
    "\n\n*[Cut off: this write-up ran past its length limit on every attempt, "
    "so the text above ends early and any later section is missing.]*"
)


def write_rationale(
    ranked: RankedPosting,
    profile_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
) -> FinalPosting:
    model = model or model_for("JOBRADAR_WRITEUP_MODEL")
    posting = ranked.scored.posting
    writeup = ranked.scored.brief_reason  # fallback if every call fails
    truncated = False
    for attempt in range(1, _WRITEUP_ATTEMPTS + 1):
        try:
            # Plain text, not structured output. As a JSON string field the
            # write-up ended at the first unescaped quote the model wrote: the
            # grammar read it as the string's end, closed the object, and the
            # call finished normally, so neither the length check below nor its
            # note fired. 18 of 230 write-ups from July to November 2026 stopped
            # mid-sentence that way, right where a quotation from the posting
            # was about to start ("...satisfies the").
            response = client.messages.create(
                model=model,
                # The rationale is a 3-section write-up (fit / highlights /
                # concerns). 1024 truncated the longer ones mid-sentence; 2048
                # still does occasionally, which the retry and the visible note
                # below handle.
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": profile_block,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": _WRITEUP_PROMPT.format(
                            tier=ranked.tier,
                            best_cv=ranked.scored.best_cv,
                            title=posting.title,
                            company=posting.company,
                            location=posting.location_text or "(not given)",
                            description=posting.description[:8000],
                            brief_reason=ranked.scored.brief_reason,
                        ),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the brief reason rather than failing the run
            logger.warning(
                "Write-up attempt %d/%d failed for %s: %s",
                attempt, _WRITEUP_ATTEMPTS, posting.url, exc,
            )
            continue
        if usage_acc is not None:
            usage.accumulate(usage_acc, response)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            logger.warning(
                "Write-up attempt %d/%d returned nothing usable for %s",
                attempt, _WRITEUP_ATTEMPTS, posting.url,
            )
            continue
        writeup = _demote_headings(text)
        truncated = response.stop_reason == "max_tokens"
        if not truncated:
            break
        logger.warning(
            "Write-up attempt %d/%d for %s hit max_tokens",
            attempt, _WRITEUP_ATTEMPTS, posting.url,
        )
    if truncated:
        writeup = writeup.rstrip() + _TRUNCATION_NOTE

    return FinalPosting(scored=ranked.scored, tier=ranked.tier, writeup=writeup)


def write_rationales(
    ranked_postings: list[RankedPosting],
    profile_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
) -> list[FinalPosting]:
    model = model or model_for("JOBRADAR_WRITEUP_MODEL")
    usage_acc: dict = {}
    finals = [
        write_rationale(r, profile_block, client, model, usage_acc=usage_acc)
        for r in ranked_postings
    ]
    usage.log_summary(logger, "writeup", usage_acc)
    return finals

"""Detailed, application-ready rationale for the few postings that actually
made the cut (ranking.select_matches() output) — the "few, high-quality"
half of the cheap-bulk-pass / expensive-finalist-pass split. Uses Sonnet
since the quality of the explanation matters more here than at the bulk
scoring stage.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel

from .. import usage
from ..llm import MODEL_DEFAULTS, model_for
from ..models import FinalPosting
from .ranking import RankedPosting

logger = logging.getLogger(__name__)

# Overridable per run via JOBRADAR_WRITEUP_MODEL; see jobradar.llm.
DEFAULT_WRITEUP_MODEL = MODEL_DEFAULTS["JOBRADAR_WRITEUP_MODEL"]


class _WriteupOutput(BaseModel):
    writeup: str


_WRITEUP_PROMPT = """\
Write an application-ready rationale for why this job posting is a {tier} match \
for the candidate, using their CV "{best_cv}" and their stated career identity.

Cover, in a few short paragraphs:
- Why it fits (skills + interest), referencing specifics from the posting.
- What to highlight when applying (most relevant experience/achievements).
- Any concerns or gaps worth being aware of going in.

Keep it concise and concrete — no generic filler.

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
            response = client.messages.parse(
                model=model,
                # The rationale is a 3-section write-up (fit / highlights / concerns)
                # plus JSON-structure overhead. 1024 truncated the longer ones
                # mid-sentence; 2048 still does occasionally, which the retry and
                # the visible note below handle.
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
                output_format=_WriteupOutput,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the brief reason rather than failing the run
            logger.warning(
                "Write-up attempt %d/%d failed for %s: %s",
                attempt, _WRITEUP_ATTEMPTS, posting.url, exc,
            )
            continue
        if usage_acc is not None:
            usage.accumulate(usage_acc, response)
        if response.parsed_output is None:
            logger.warning(
                "Write-up attempt %d/%d returned nothing usable for %s",
                attempt, _WRITEUP_ATTEMPTS, posting.url,
            )
            continue
        writeup = response.parsed_output.writeup
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

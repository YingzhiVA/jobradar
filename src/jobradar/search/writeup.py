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


def write_rationale(
    ranked: RankedPosting,
    profile_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
) -> FinalPosting:
    model = model or model_for("JOBRADAR_WRITEUP_MODEL")
    posting = ranked.scored.posting
    writeup = ranked.scored.brief_reason  # fallback if the call below fails
    try:
        response = client.messages.parse(
            model=model,
            # The rationale is a 3-section write-up (fit / highlights / concerns)
            # plus JSON-structure overhead; 1024 truncated the longer ones
            # mid-sentence, so give it real headroom.
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
        if usage_acc is not None:
            usage.accumulate(usage_acc, response)
        if response.stop_reason == "max_tokens":
            # Surface truncation instead of silently shipping a cut-off rationale.
            logger.warning("Write-up for %s hit max_tokens and may be truncated", posting.url)
        if response.parsed_output is not None:
            writeup = response.parsed_output.writeup
    except Exception as exc:  # noqa: BLE001 - fall back to the brief reason rather than failing the run
        logger.warning("Write-up failed for %s: %s", posting.url, exc)

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

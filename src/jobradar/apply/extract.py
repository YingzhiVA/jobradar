"""Turn noisy scraped page text into a clean job description plus the
structured facts the rest of the pipeline needs (title, company, language,
whether a cover letter is asked for).

This is a mechanical clean-up-and-classify task, not a judgment call, so it
runs on Haiku (the "less mission-critical" half of the model split).
"""

from __future__ import annotations

import logging
from typing import Callable

import anthropic
from pydantic import BaseModel

from .. import usage
from ..llm import MODEL_DEFAULTS, model_for
from .util import drain_stream

logger = logging.getLogger(__name__)

# Overridable via JOBRADAR_APPLY_HELPER_MODEL, resolved per call; see jobradar.llm.
HELPER_MODEL = MODEL_DEFAULTS["JOBRADAR_APPLY_HELPER_MODEL"]

# Page text beyond this is nav/footer/legal noise on any real posting page;
# capping bounds the Haiku input cost per URL.
_MAX_PAGE_CHARS = 30_000


class JDExtract(BaseModel):
    is_job_posting: bool
    title: str = ""
    company: str = ""
    location: str = ""
    # Swiss postal code of the work location, used by the optional RAV
    # proof-of-applications table (rav.enabled in config/search.yaml).
    # Approximate is fine (any postcode of the right town).
    postcode: str = ""
    # ISO 639-1 code of the posting's main language ("en", "de", "fr", ...).
    # The cover letter is written in this language.
    language: str = "en"
    # True only when the posting explicitly asks for a cover/motivation letter
    # (incl. "Motivationsschreiben" / "lettre de motivation").
    cover_letter_required: bool = False
    description_markdown: str = ""


_EXTRACT_PROMPT = """\
Below is the visible text scraped from a web page that should contain a
single job posting.

If it does, reproduce that posting faithfully as clean markdown in
description_markdown: keep every responsibility, requirement, qualification,
benefit, and application instruction; drop navigation, cookie banners,
footers, unrelated "other openings" teasers, and legal boilerplate. Do NOT
summarize, shorten, or paraphrase the posting's content - this text is the
ground truth a CV will be tailored against.

Also fill in:
- title, company, location: as stated in the posting.
- postcode: the postal code of that work location. Use the one printed in the
  posting if there is one; otherwise give a postal code of the named town from
  your own knowledge (e.g. "8001" for a posting that just says Zurich, "3011"
  for Bern). It only has to identify the town, not the exact street, so pick a
  central one. Leave it empty if the location is unclear, remote-only, or
  outside Switzerland.
- language: ISO 639-1 code of the language the posting is mainly written in.
- cover_letter_required: true only if the posting or its application
  instructions explicitly ask for a cover letter / motivation letter
  (Motivationsschreiben, lettre de motivation). Merely listing generic
  "application documents" does not count.

Set is_job_posting=false if the page is an error page, an expired-posting
notice, a search-results/list page, or otherwise does not contain one
specific job posting.

Page text:
{page_text}
"""


def extract_jd(
    page_text: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> JDExtract | None:
    """None means the call itself failed; is_job_posting=False means the page
    had no usable posting. Callers treat both as "needs a manual JD paste".

    Streams for the same reason the writing calls do (see tailor.py): the
    posting is reproduced in full, so a long JD is a long call, and `on_delta`
    is what lets the caller show it arriving.
    """
    model = model or model_for("JOBRADAR_APPLY_HELPER_MODEL")
    try:
        with client.messages.stream(
            model=model,
            max_tokens=8192,
            messages=[
                {
                    "role": "user",
                    "content": _EXTRACT_PROMPT.format(page_text=page_text[:_MAX_PAGE_CHARS]),
                }
            ],
            output_format=JDExtract,
        ) as stream:
            drain_stream(stream, on_delta)
            response = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - best-effort per URL
        logger.warning("JD extraction failed: %s", exc)
        return None
    if usage_acc is not None:
        usage.accumulate(usage_acc, response)
    if response.stop_reason == "max_tokens":
        logger.warning("JD extraction hit max_tokens; description may be truncated")
    return response.parsed_output

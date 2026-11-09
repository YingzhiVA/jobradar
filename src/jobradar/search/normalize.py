"""Turn a RawPosting into a structured Posting.

Cheap regex/keyword heuristics run first and cost nothing. Only when the
fields that actually drive hard filtering (employment_pct,
office_days_per_week) are still unknown after that does this fall back to
one small Haiku 5.5 extraction call per posting — and only if an Anthropic
client was supplied (callers that don't have/want to spend API calls, e.g.
tests or a quick dry run, can pass client=None and just get the heuristic
result).
"""

from __future__ import annotations

import logging
import re

import anthropic
from pydantic import BaseModel

from ..markets import CH, Market
from ..models import Constraints, Posting, posting_id
from .filters import names_a_place, remote_country_ok
from .sources.base import RawPosting

logger = logging.getLogger(__name__)

# Haiku 5.5 since 1.14.0 (dev/fill-eval/NOTES.md): on 80 real postings it made
# the same filter decisions as Haiku 4.5 at about a sixth of the cost, and it
# leaves a workload the posting never states as unknown instead of guessing
# 100%, which dropped part-time searchers' postings. Hardcoded rather than read
# from .env because the request options below are Haiku 5.5's: Haiku 4.5
# rejects `effort`.
_EXTRACTION_MODEL = "claude-haiku-5-5"
# Haiku 5.5 thinks before it answers and thinking counts against max_tokens: at
# the old 256, 29 of 80 test calls ran out of room and came back unusable, with
# no error, so the posting kept its gaps (one in-range posting was dropped on
# location that way). Low effort keeps the thinking short; the largest answer
# in testing was 492 tokens, and the cap itself is not billed.
#
# Thinking is NOT disabled: that variant placed a posting in Tampere, Finland
# in canton Vaud, citing "Tampere" as its source, which the traceability guard
# below cannot catch.
_EXTRACTION_MAX_TOKENS = 4096
_EXTRACTION_EFFORT = "low"

_PCT_RANGE_RE = re.compile(r"(\d{1,3})\s*(?:%|\bpercent\b)?\s*(?:-|to|–)\s*(\d{1,3})\s*%")
_PCT_SINGLE_RE = re.compile(r"(\d{1,3})\s*%")
_OFFICE_DAYS_RE = re.compile(
    r"(\d)\s*days?\s*(?:a|per)?\s*week[^.]{0,40}?(?:office|on-?site)"
    r"|(?:office|on-?site)[^.]{0,40}?(\d)\s*days?\s*(?:a|per)?\s*week",
    re.I,
)
_SPONSOR_NEGATIVE_RE = re.compile(
    r"(?:cannot|can't|unable to|do not|don't|no)\s+(?:provide\s+)?(?:visa\s+)?sponsor", re.I
)
_SPONSOR_POSITIVE_RE = re.compile(r"(?:visa\s+)?sponsor(?:ship)?\s+(?:is\s+)?available|we\s+sponsor", re.I)

def _heuristic_remote(location_text: str, description: str) -> bool | None:
    haystack = f"{location_text} ".lower()
    if "remote" in haystack:
        return True
    if "hybrid" in haystack or "on-site" in haystack or "onsite" in haystack:
        return False
    return None


def _resolve_remote(raw: RawPosting) -> bool | None:
    """The source's own structured remote flag when it set one, else the text
    heuristic. A board that states the workplace type outranks sniffing its
    location string — the string is often just a country ("Switzerland"), which
    the heuristic can't read anything out of.
    """
    if raw.remote is not None:
        return raw.remote
    return _heuristic_remote(raw.location or "", raw.description)


def _heuristic_employment_pct(text: str) -> tuple[int, int] | None:
    """Returns (min, max) workload percentage the posting offers, e.g.
    "80-100%" -> (80, 100); a single figure or "full-time" -> (x, x).
    """
    # A percentage range/figure is more precise than the bare "full-time"
    # keyword, so check for it first and only fall back to the keyword.
    range_match = _PCT_RANGE_RE.search(text)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        return (min(lo, hi), max(lo, hi))
    single_match = _PCT_SINGLE_RE.search(text)
    if single_match:
        value = int(single_match.group(1))
        return (value, value)
    if re.search(r"\bfull[\s-]?time\b", text, re.I):
        return (100, 100)
    return None


def _heuristic_office_days(text: str) -> int | None:
    match = _OFFICE_DAYS_RE.search(text)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return int(value)


def _heuristic_sponsorship(text: str) -> bool | None:
    if _SPONSOR_NEGATIVE_RE.search(text):
        return False
    if _SPONSOR_POSITIVE_RE.search(text):
        return True
    return None


def heuristic_normalize(raw: RawPosting, market: Market = CH) -> Posting:
    full_text = f"{raw.location or ''} {raw.description}"
    pct_range = _heuristic_employment_pct(full_text)
    return Posting(
        id=posting_id(raw.url, raw.title, raw.company, raw.source),
        source=raw.source,
        url=raw.url,
        title=raw.title,
        company=raw.company,
        description=raw.description,
        location_text=raw.location,
        # Canton is resolved from the location field alone, not the full
        # description: descriptions routinely mention other Swiss cities in
        # passing (company HQ, other office locations — e.g. "headquarters
        # in St. Gallen... offices in London and New York"), which would
        # otherwise misattribute a non-Swiss posting's canton.
        canton=market.region_in(raw.location or ""),
        remote=_resolve_remote(raw),
        employment_pct_min=pct_range[0] if pct_range else None,
        employment_pct_max=pct_range[1] if pct_range else None,
        office_days_per_week=_heuristic_office_days(full_text),
        sponsorship_mentioned=_heuristic_sponsorship(full_text),
        liveness=raw.liveness,
    )


class _ExtractedFields(BaseModel):
    employment_pct_min: int | None = None
    employment_pct_max: int | None = None
    office_days_per_week: int | None = None
    remote: bool | None = None
    sponsorship_mentioned: bool | None = None
    canton: str | None = None
    # The substring of the location line the model read the canton off. Only
    # here so _canton_from_location() can verify the canton actually came from
    # the location rather than from the description; never stored on Posting.
    canton_source: str | None = None


_EXTRACTION_PROMPT = """\
Extract these fields from the job posting below. Use null when the posting \
doesn't say:
- employment_pct_min / employment_pct_max: the workload percentage range \
the posting offers, e.g. "80-100%" -> min 80, max 100. A single figure or \
"full-time" -> min and max both that figure (100 for full-time).
- office_days_per_week: required in-office days per week, as a number.
- remote: true if the role is remote (fully or "remote-friendly"), false if \
explicitly on-site/hybrid-only, null if unclear.
- sponsorship_mentioned: true if visa/work-permit sponsorship is explicitly \
offered, false if explicitly NOT offered, null if not mentioned.
{region_fields}

Title: {title}
Location: {location}
Description:
{description}
"""


def _canton_from_location(extracted: _ExtractedFields, posting: Posting) -> str | None:
    """The extracted canton, but only if it demonstrably came from the posting's
    location rather than from its description.

    The model is shown the whole description because the other fields need it,
    which leaves canton exposed to boilerplate like "headquarters in St. Gallen,
    Switzerland, and offices in London and New York" — read off a London
    posting, that resolves a Swiss canton and walks it straight through the
    location filter. heuristic_normalize() avoids this by only ever looking at
    the location field; this is the same rule enforced on the LLM's answer,
    deterministically, rather than trusted to prompt compliance alone.
    """
    if extracted.canton is None:
        return None
    location_text = (posting.location_text or "").strip()
    if not location_text:
        # Nothing to verify against: the description was the only location
        # signal available, and could_pass_location() deliberately lets these
        # through to be resolved here rather than dropping them unseen.
        return extracted.canton
    source = (extracted.canton_source or "").strip()
    if source and source.lower() in location_text.lower():
        return extracted.canton
    logger.info(
        "Ignoring canton %r for %s: not traceable to location %r (source=%r)",
        extracted.canton,
        posting.url,
        location_text,
        extracted.canton_source,
    )
    return None


# Copied from the extraction only where the heuristics left them unknown. canton
# is handled separately, since it must first pass _canton_from_location.
_GAP_FIELDS = (
    "employment_pct_min",
    "employment_pct_max",
    "office_days_per_week",
    "remote",
    "sponsorship_mentioned",
)


def llm_fill_gaps(
    posting: Posting, client: anthropic.Anthropic, market: Market = CH
) -> Posting:
    """Fills employment_pct_min/max, office_days_per_week, and canton (and
    remote/sponsorship if still unknown) via one small structured-output
    call. Only call this when heuristic_normalize() left something
    filter-relevant unresolved.
    """
    try:
        response = client.messages.parse(
            model=_EXTRACTION_MODEL,
            max_tokens=_EXTRACTION_MAX_TOKENS,
            output_config={"effort": _EXTRACTION_EFFORT},
            messages=[
                {
                    "role": "user",
                    "content": _EXTRACTION_PROMPT.format(
                        title=posting.title,
                        location=posting.location_text or "(not given)",
                        description=posting.description[:4000],
                        region_fields=market.region_prompt.format(
                            regions=", ".join(market.regions)
                        ),
                    ),
                }
            ],
            output_format=_ExtractedFields,
        )
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        logger.warning("LLM gap-fill failed for %s: %s", posting.url, exc)
        return posting

    extracted = response.parsed_output
    if extracted is None:
        return posting

    if posting.canton is None:
        posting.canton = _canton_from_location(extracted, posting)
        if posting.canton is not None:
            posting.llm_filled.append("canton")
    for name in _GAP_FIELDS:
        value = getattr(extracted, name)
        if getattr(posting, name) is None and value is not None:
            setattr(posting, name, value)
            posting.llm_filled.append(name)
    return posting


def _needs_canton(posting: Posting, constraints: Constraints | None) -> bool:
    """Whether canton resolution is actually worth an LLM call for this
    posting. False when:
    - allowed_cantons isn't configured at all (filters.py never looks at
      canton, so there's nothing to resolve it for), or
    - heuristic_normalize() already resolved it, or
    - the posting already passes the location check on either remote branch
      (constraints.remote_ok with posting.remote True, or a remote posting
      inside constraints.remote_countries) — mirrors filters._location_ok()'s
      own short-circuits, so a confirmed-remote posting never needs a canton
      to pass. The remote_countries case is the one that actually saves money,
      but only for a country-level location ("Switzerland"), where the LLM could
      never resolve a canton and every call would be wasted. A remote posting
      that names a town still gets resolved — see filters.names_a_place.
    """
    if constraints is None or not constraints.allowed_cantons:
        return False
    if posting.canton is not None:
        return False
    if constraints.remote_ok and posting.remote is True:
        return False
    # Skip resolution only for a genuinely country-level location, where the LLM
    # could never find a canton anyway. A remote posting that names a town still
    # needs its canton: filters.remote_country_ok stands aside once one is
    # resolved, so that is what keeps a remote role in an excluded canton out.
    if remote_country_ok(posting, constraints) and not names_a_place(
        posting.location_text, constraints
    ):
        return False
    return True


def fill_gaps(
    posting: Posting,
    client: anthropic.Anthropic | None,
    *,
    constraints: Constraints | None = None,
    market: Market = CH,
) -> Posting:
    """Run the (paid) LLM gap-fill on an already heuristic-normalized posting,
    but only when something filter-relevant is still missing. Split out from
    normalize() so callers can dedup on the cheap heuristic result first and
    pay for gap-filling only on the postings that survive dedup.
    """
    needs_llm = (
        posting.employment_pct_max is None
        or posting.office_days_per_week is None
        or _needs_canton(posting, constraints)
    )
    if needs_llm and client is not None:
        posting = llm_fill_gaps(posting, client, market)
    return posting


def normalize(
    raw: RawPosting,
    client: anthropic.Anthropic | None,
    *,
    constraints: Constraints | None = None,
    market: Market = CH,
) -> Posting:
    """constraints: pass the run's Constraints so canton resolution can be
    skipped when filters.py wouldn't need it anyway (see _needs_canton).
    Pass None (the default) to never trigger canton resolution — e.g. in
    tests, or callers that don't filter by canton at all.
    """
    return fill_gaps(
        heuristic_normalize(raw, market), client, constraints=constraints, market=market
    )

"""Deterministic hard-constraint filtering. No LLM calls here — every check
is a plain comparison against config/constraints.yaml, and a posting either
passes all of them or it's dropped before it ever reaches the (paid) scoring
stage.

Fields with unknown/missing values are treated permissively (the check is
skipped) rather than causing a drop, except for location: location is the
one constraint where "we genuinely can't tell" is treated the same as "no
match", since shipping a posting in the wrong country/city is the kind of
mistake this tool exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Constraints, Posting


@dataclass
class FilterResult:
    posting: Posting
    passed: bool
    failed_checks: list[str]


def _canton_key(name: str) -> str:
    # Drop spaces/periods so "St.Gallen" (as a user might type it in
    # constraints.yaml) and "St. Gallen" (normalize.py's canonical name)
    # compare equal.
    return re.sub(r"[.\s]", "", name.lower())


def _canton_ok(posting: Posting, constraints: Constraints) -> bool:
    if not constraints.allowed_cantons or not posting.canton:
        return False
    posting_canton = _canton_key(posting.canton)
    # Substring-containment rather than exact equality, to tolerate naming
    # variants like "Basel" (constraint) vs "Basel-Stadt" (resolved canton).
    return any(
        _canton_key(c) in posting_canton or posting_canton in _canton_key(c)
        for c in constraints.allowed_cantons
    )


# Country-name variants worth accepting in a location line, so a board that
# writes "Schweiz" or "Suisse" still matches a remote_countries entry of
# "Switzerland". Only the country this tool is actually scoped to needs one;
# anything else falls back to a plain substring match on the configured name.
_COUNTRY_ALIASES = {
    "switzerland": ("switzerland", "schweiz", "suisse", "svizzera"),
}


def remote_country_ok(posting: Posting, constraints: Constraints) -> bool:
    """Whether this is a remote posting scoped to one of constraints.
    remote_countries.

    The point is boards that give a remote role a country-level location and
    nothing finer — Jobgether's Lever board lists 176 Swiss roles as plainly
    "Switzerland", with no town anywhere in the text — so canton resolution has
    nothing to work with and _canton_ok can never pass them. A commute radius
    is meaningless for a role with no office to commute to anyway; what matters
    is that the employer wants someone based in the country.

    Read off the location line only, never the description: same rule as canton
    resolution, and for the same reason — descriptions name other countries in
    passing (head offices, customer regions), and a remote US role whose blurb
    mentions a Swiss parent must not pass on that alone.
    """
    if not constraints.remote_countries or posting.remote is not True:
        return False
    location = (posting.location_text or "").lower()
    if not location.strip():
        return False  # can't place it — treated as no match, like every other location check
    for country in constraints.remote_countries:
        key = country.strip().lower()
        for variant in _COUNTRY_ALIASES.get(key, (key,)):
            if variant in location:
                return True
    return False


def _location_ok(posting: Posting, constraints: Constraints) -> bool:
    if constraints.remote_ok and posting.remote is True:
        return True
    if remote_country_ok(posting, constraints):
        return True
    if _canton_ok(posting, constraints):
        return True
    haystack = f"{posting.location_text or ''} {posting.description}".lower()
    if any(city.lower() in haystack for city in constraints.allowed_cities):
        return True
    if any(country.lower() in haystack for country in constraints.allowed_countries):
        return True
    return False


# Words that signal a posting could be in Switzerland even when its town isn't
# in normalize.py's heuristic canton dict — so it's worth an LLM canton call.
_SWISS_SIGNALS = ("switzerland", "schweiz", "suisse", "svizzera")


def could_pass_location(posting: Posting, constraints: Constraints) -> bool:
    """Cheap pre-LLM gate: would it be worth spending a canton-resolution LLM
    call on this (heuristic-only) posting, because it could plausibly pass the
    location filter? Lets the pipeline skip the LLM gap-fill for clearly-foreign
    postings (e.g. a Databricks role in San Francisco) that would be dropped on
    location regardless. Conservative — returns True whenever in doubt.

    Canton resolution is the *only* location-relevant thing the LLM adds, so a
    posting that already fails _location_ok can only be rescued if (a) cantons
    are configured and (b) it might actually be in Switzerland.
    """
    if _location_ok(posting, constraints):
        return True
    if not constraints.allowed_cantons:
        return False  # no canton list → LLM canton resolution can't help
    if not (posting.location_text or "").strip():
        return True  # location unknown — don't silently drop before the LLM sees it
    haystack = f"{posting.location_text} {posting.description}".lower()
    return any(sig in haystack for sig in _SWISS_SIGNALS)


# A posting whose employer we can't identify is not actionable — you can't
# research it, tailor an application, or judge culture fit — so drop it before
# it costs an LLM gap-fill/scoring pass. web_search sometimes returns these
# (e.g. "Undisclosed", "Unknown Banking/Financial Services", "Stealth startup").
_UNDISCLOSED_SUBSTRINGS = ("undisclosed", "unknown", "confidential", "not disclosed", "stealth")
_UNDISCLOSED_EXACT = {"n/a", "na", "tbd", "none", "-", "?"}


def company_is_named(posting: Posting) -> bool:
    """False when the posting's employer is missing or explicitly undisclosed."""
    name = (posting.company or "").strip().lower()
    if not name or name in _UNDISCLOSED_EXACT:
        return False
    return not any(s in name for s in _UNDISCLOSED_SUBSTRINGS)


def _employment_pct_ok(posting: Posting, constraints: Constraints) -> bool:
    if posting.employment_pct_min is None or posting.employment_pct_max is None:
        return True  # unknown — don't drop on missing data
    # Range overlap test: the posting's [min, max] and the constraint's
    # [min, max] must intersect.
    return (
        posting.employment_pct_max >= constraints.min_percentage
        and posting.employment_pct_min <= constraints.max_percentage
    )


def _office_days_ok(posting: Posting, constraints: Constraints) -> bool:
    if constraints.max_office_days_per_week is None or posting.office_days_per_week is None:
        return True
    return posting.office_days_per_week <= constraints.max_office_days_per_week


def evaluate(posting: Posting, constraints: Constraints) -> FilterResult:
    failed = []
    if not _location_ok(posting, constraints):
        failed.append("location")
    if not _employment_pct_ok(posting, constraints):
        failed.append("employment_pct")
    if not _office_days_ok(posting, constraints):
        failed.append("office_days")
    return FilterResult(posting=posting, passed=not failed, failed_checks=failed)


def filter_postings(
    postings: list[Posting], constraints: Constraints
) -> tuple[list[Posting], list[FilterResult]]:
    """Returns (postings that passed every check, every result for logging)."""
    results = [evaluate(p, constraints) for p in postings]
    kept = [r.posting for r in results if r.passed]
    return kept, results

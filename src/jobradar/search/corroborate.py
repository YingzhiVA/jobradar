"""Cross-source corroboration: check a web_search lead against the employer's
own job listing, when this same run happens to have fetched one.

The gap this closes was visible in the 2026-08-17 run. web_search returned two
Swiss Re roles on www.swissre.com; both were closed (410), but that host 403s the
CI runner's IP, so liveness.py couldn't tell and kept them, and they reached the
report as live matches. Meanwhile the *same run* had pulled Swiss Re's actual ATS
(careers.swissre.com) and listed 24 open roles — neither of the two among them.
The run held direct evidence against its own output and nothing read it.

That evidence is not proof on its own. An ATS listing can be paginated short, a
company can post a role on a board we don't scan, and titles get reworded between
a search snippet and the posting itself — so absence from the listing is a weak
signal, and this module never drops anything by itself. It records a verdict; the
caller decides. The intended use (see main.py) is to require agreement with a
second weak signal — a liveness check that also couldn't confirm the link — before
dropping. Two independent weak signals pointing the same way is the strong one.

Matching is deliberately conservative in both directions:
  * Only postings from a source that lists ONE employer's own openings count as
    a listing to check against (company_pages). A multi-employer board (eth_jobs)
    is not that employer's listing, so its absence proves nothing.
  * A company with no listing this run gets CORROBORATION_NONE — no opinion.
  * Title matching requires near-identity after normalization, because the near
    misses are exactly the trap: Swiss Re's real listing had an "Analytics Product
    Expert" and an "AI Engineer", which a loose token overlap would happily accept
    as corroborating "AI & Analytics Product Engineer". A generous matcher here
    fails toward silence, which is the failure this module exists to fix.
"""

from __future__ import annotations

import logging
import re

from .sources.base import (
    CORROBORATION_ABSENT,
    CORROBORATION_NONE,
    CORROBORATION_PRESENT,
    FetchResult,
    RawPosting,
)

logger = logging.getLogger(__name__)

# Sources whose postings are one employer's own listing of its own openings, and
# therefore usable as a reference to check a third-party lead against. Named by
# FetchResult.name (the connector), not RawPosting.source (the ATS vendor).
LISTING_SOURCES = frozenset({"company_pages"})

# Sources whose leads are worth checking — links from the open web, where the
# staleness problem lives. A posting from a listing source corroborates itself.
LEAD_SOURCES = frozenset({"web_search"})

# Fraction of the combined token vocabulary two titles must share to count as the
# same role. High on purpose: 0.8 accepts wording and punctuation drift
# ("AI/ML Product Manager" vs "AI ML Product Manager") but rejects the
# same-neighbourhood-different-role case (0.6 for the Swiss Re pair above).
_TITLE_MATCH_THRESHOLD = 0.8

# Title noise that says nothing about which role this is: seniority, contract
# shape, and the gender/workload markers Swiss postings carry in the title.
_TITLE_STOPWORDS = frozenset({
    "senior", "junior", "lead", "principal", "staff", "head", "chief",
    "hybrid", "remote", "onsite", "on", "site", "fulltime", "parttime",
    "full", "part", "time", "permanent", "temporary", "intern", "internship",
    "m", "f", "x", "d", "w", "mfxd", "fmxd",
    "the", "a", "an", "and", "or", "of", "for", "in", "at", "to", "with",
})

# Company-name noise: legal form and group suffixes that differ between how a
# search result names an employer and how its own careers page does.
_COMPANY_STOPWORDS = frozenset({
    "ag", "sa", "sarl", "gmbh", "ltd", "limited", "llc", "inc", "incorporated",
    "plc", "bv", "nv", "spa", "srl", "oy", "ab", "as", "co", "company",
    "group", "holding", "holdings", "international", "global", "the",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str, stopwords: frozenset[str]) -> frozenset[str]:
    """Lowercase alphanumeric tokens with the given noise words removed.

    Percentage figures ("80-100%") tokenize to bare numbers, which carry no role
    identity and would otherwise inflate overlap between two unrelated postings
    that happen to share a workload range, so digit-only tokens go too.
    """
    return frozenset(
        t
        for t in _TOKEN_RE.findall(text.lower())
        if t not in stopwords and not t.isdigit()
    )


def _company_key(name: str) -> str:
    """A comparable form of a company name, ignoring legal form and word order."""
    return " ".join(sorted(_tokens(name, _COMPANY_STOPWORDS)))


def _titles_match(lead_title: str, listed_title: str) -> bool:
    """Whether two titles name the same role, by Jaccard overlap of their
    significant tokens. Empty on either side never matches — an unnamed role
    can't corroborate anything.
    """
    a = _tokens(lead_title, _TITLE_STOPWORDS)
    b = _tokens(listed_title, _TITLE_STOPWORDS)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= _TITLE_MATCH_THRESHOLD


def build_listings(fetch_results: list[FetchResult]) -> dict[str, list[RawPosting]]:
    """Index this run's employer-listing postings by normalized company name.

    Only sources in LISTING_SOURCES contribute; a company absent from the result
    simply wasn't listed this run, which is not evidence about anything.
    """
    listings: dict[str, list[RawPosting]] = {}
    for result in fetch_results:
        if result.name not in LISTING_SOURCES or not result.ok:
            # A degraded listing source may have fetched a partial or empty set
            # of a company's roles, which would manufacture false absences.
            continue
        for posting in result.postings:
            key = _company_key(posting.company)
            if key:
                listings.setdefault(key, []).append(posting)
    return listings


def corroborate(fetch_results: list[FetchResult]) -> dict[str, int]:
    """Stamp every lead posting's `corroboration` against this run's employer
    listings, in place. Returns a count per verdict for the run log.

    Nothing is dropped here — see the module docstring on why absence alone is
    too weak to act on.
    """
    listings = build_listings(fetch_results)
    counts = {CORROBORATION_NONE: 0, CORROBORATION_PRESENT: 0, CORROBORATION_ABSENT: 0}

    for result in fetch_results:
        if result.name not in LEAD_SOURCES:
            continue
        for lead in result.postings:
            listed = listings.get(_company_key(lead.company))
            if not listed:
                lead.corroboration = CORROBORATION_NONE
            elif any(_titles_match(lead.title, p.title) for p in listed):
                lead.corroboration = CORROBORATION_PRESENT
            else:
                lead.corroboration = CORROBORATION_ABSENT
                logger.info(
                    "corroborate: %s — %s is absent from %s's own listing "
                    "(%d roles fetched this run)",
                    lead.company,
                    lead.title,
                    lead.company,
                    len(listed),
                )
            counts[lead.corroboration] += 1

    return counts

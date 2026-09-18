"""Profile-driven discovery source: uses Claude's server-side web_search
tool to find postings beyond the curated company list, by searching against
*what the candidate wants and is good at* rather than a fixed keyword list.

This is the source that delivers the tool's core promise — surfacing
genuinely-fitting roles whose titles you'd never think to put in a keyword
alert (e.g. "Associate to the Directors", "User Journey Strategist"). It
hands the search engine (itself an LLM) the candidate's identity statement
and the kinds of roles they've previously applied to, and asks it to judge
fit by substance, not title keywords. Discovery is therefore NOT pre-gated
by keywords; the later scoring stage still decides actual fit.

web_search is a server-side tool: Claude executes the search itself and
returns a grounded answer in one request/response, so no manual tool-use
loop is needed here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlsplit

import anthropic
from pydantic import BaseModel, ValidationError

from ..liveness import filter_live
from ...llm import model_for, web_search_tool_type
from .base import (
    LIVENESS_CONFIRMED,
    LIVENESS_UNVERIFIED,
    FetchResult,
    RawPosting,
)

logger = logging.getLogger(__name__)

# Aggregators / job-board mirrors that routinely serve listings which closed
# months ago and — unlike a direct ATS/employer page — DON'T 404 when the role is
# gone (they return a 200 "no longer available" page, or bot-block the liveness
# check entirely). The prompt already steers the model away from these, but that
# steer is soft; drop them in code so a stale aggregator link can't reach scoring
# or the report regardless of what the model returns. The trade-off is deliberate:
# we'd rather lose the occasional role whose only surfaced link is an aggregator
# than surface a dead one — the direct posting is usually discoverable elsewhere.
# Matched on host suffix so subdomains (www., de., ch.) are covered.
_AGGREGATOR_HOSTS = (
    "datacareer.ch",
    "jobs.ch",
    "jobup.ch",
    "jobscout24.ch",
    "ostjob.ch",
    "indeed.com",
    "indeed.ch",
    "glassdoor.com",
    "glassdoor.ch",
    "linkedin.com",
    "levels.fyi",
    "monster.com",
    "monster.ch",
    "stepstone.com",
    "stepstone.de",
    "xing.com",
)


def _is_aggregator(url: str) -> bool:
    """True if the URL's host is (or is a subdomain of) a known aggregator/mirror."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


def _is_pathless(url: str) -> bool:
    """True if the URL is a bare domain root with nothing identifying a specific
    posting — empty path (or just "/") AND no query string.

    Such a link is never a real permalink; it's the careers-board homepage the
    model handed back when it couldn't (or didn't) surface the deep link. Two
    reasons to drop it before scoring: (1) it hashes to a *different* dedup id
    than the same role's real /job/view/... permalink from a direct source, so
    it defeats de-duplication (the ETH AI Center repeat on 2026-07-09); and
    (2) it would ship a useless "apply here" link pointing at a board homepage.
    A query string is kept as meaningful because some ATSs put the job id there
    (e.g. ?gh_jid=123), so root-with-query is still a specific posting.
    """
    parts = urlsplit(url)
    return parts.path.strip("/") == "" and not parts.query

# Discovery model. Defaulting to Haiku: in practice it's the MORE reliable tier
# here — Haiku + the basic web_search_20250305 completes in ~1-2 min, while
# Sonnet + dynamic-filtering (web_search_20260209) repeatedly ran 13-15 min,
# hung once for 22 min, and timed out at the 15-min cap (it pulls far more page
# content into context). Haiku's downside is lower recall; the relaxed prompt
# below aims to offset that. Set JOBRADAR_WEB_SEARCH_MODEL=claude-sonnet-4-6 to
# trade reliability for recall once Sonnet's web_search latency is sorted.
# Resolved per fetch via jobradar.llm (so a value in .env is honoured); the
# web_search tool version follows the model automatically, so switching the
# model is the only change needed to move between Haiku and Sonnet.
_MODEL_VAR = "JOBRADAR_WEB_SEARCH_MODEL"
_MAX_TOKENS = 4096

# Hard cap on web searches per run. web_search is billed at $10/1000 searches
# AND every result is pulled into the model's context as input tokens, so an
# uncapped open-ended query can run 10+ searches and multiply cost several-fold.
# Set to 5 for broader market coverage: Haiku runs the searches efficiently
# (~1-2 min total) and the prompt steers it to spend each one on a distinct
# finder angle rather than near-duplicate queries. 5 bounds cost (~500K input
# tokens) while still finishing fast enough to rarely hang. Override via
# JOBRADAR_WEB_SEARCH_MAX_USES.
_DEFAULT_MAX_SEARCHES = int(os.environ.get("JOBRADAR_WEB_SEARCH_MAX_USES", "5"))
# A real web_search call legitimately takes minutes (server-side search loop);
# Sonnet with dynamic filtering runs ~13-15 min, so the timeout must clear that
# — but stay bounded, so a stuck call fails after ~15 min instead of the SDK's
# 10-min-per-attempt × retries (which hung a run for 22+ min). max_retries=0 +
# this timeout means one bounded attempt; on timeout the source raises,
# _fetch_all catches it, and the run degrades to company_pages only rather than
# stalling. Lower this if you switch to Haiku (it finishes in ~1-2 min).
# Override via JOBRADAR_WEB_SEARCH_TIMEOUT.
_TIMEOUT_SECONDS = float(os.environ.get("JOBRADAR_WEB_SEARCH_TIMEOUT", "900"))


class _DiscoveredPosting(BaseModel):
    title: str
    company: str
    url: str
    location: str | None = None
    description: str = ""


class _DiscoveredPostings(BaseModel):
    postings: list[_DiscoveredPosting]


_PROMPT_TEMPLATE = """\
A job candidate is looking for their next role. Here is what they want and \
the kind of work they're drawn to:

{profile_intent}

Location preference: {location_desc}

You are the discovery scout for an automated pipeline. Be GENEROUS about fit \
and cast a wide net. Later stages re-check the exact location, verify each \
link is still live (and drop dead ones), de-duplicate, and score each role for \
fit — so surface the leads and let those stages prune.

On FIT, judge fit by the *substance* of the role — its responsibilities and \
the direction it represents — NOT by matching job-title keywords. Deliberately \
include strong-substance roles whose titles are unconventional or that a plain \
title search would miss. When you're unsure whether a role *fits*, INCLUDE it: \
a borderline-fit lead that a later stage discards is far cheaper than a good \
role you never surfaced. Lean toward roles in or near the location preference \
above but keep a borderline-location role rather than drop it (a later stage \
checks location precisely). The only roles worth excluding on substance are \
ones that plainly clash with what the candidate says they're moving away from.

On COVERAGE, spend each of your searches on a DIFFERENT angle so they cover \
distinct parts of the market rather than re-running near-duplicate queries. \
Vary the angle across searches — for example by role family / function, by \
seniority, by industry or problem domain, by adjacent or unconventional titles \
for the same substance, and by employer type (startup / scale-up / enterprise \
/ research lab). Each new search should reach roles the previous ones wouldn't \
have surfaced.

On LINKS, aim for quality but don't let it cost you a good lead:
- Strongly prefer the employer's own careers page or the direct ATS posting \
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters, etc.) — that link is the \
most likely to be current and directly applyable.
- Avoid third-party aggregators and cached snapshots where you can (e.g. \
levels.fyi, Glassdoor, LinkedIn job mirrors, generic job-board search pages); \
they often serve listings that closed months ago. Favor roles that look \
recently posted.
- Give the most direct, specific URL you can find. If the only link you can \
find for an otherwise strong-fit role is imperfect, still include it rather \
than drop the role — the downstream liveness check will catch a dead link.

Respond with ONLY a JSON object (no prose, no markdown fences) matching this shape:
{{"postings": [{{"title": str, "company": str, "url": str, "location": str | null, \
"description": str}}]}}

Only return {{"postings": []}} if you genuinely found no plausibly-fitting \
roles after actually searching.
"""


def _extract_json(text: str) -> dict:
    """Pull the postings JSON object out of the model's answer text.

    The prompt asks for a bare JSON object, but the web_search tool tends to
    wrap it in narration and/or markdown fences, so we tolerate both: prefer a
    fenced ```json block if present, else fall back to the outermost {...}.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("no JSON object found in response")
        candidate = match.group(0)
    return json.loads(candidate)


def _parse_postings(text_blocks: list[str]) -> list[_DiscoveredPosting]:
    """Join the response's text blocks and parse the postings out of them.

    Citations are always on for web search, which splits the model's answer
    across many text blocks — so the JSON object may live in an earlier block
    or straddle several. Joining all blocks reconstructs the full answer text
    before extraction. Raises ValueError/ValidationError on unparseable output.
    """
    return _DiscoveredPostings.model_validate(_extract_json("\n".join(text_blocks))).postings


def _search_count(response) -> int:
    """How many web searches the server-side tool actually ran.

    This is the key observability signal: a healthy empty result still shows
    >=1 search ("I looked and found nothing"), whereas 0 searches means the
    model never even queried the web — so an empty result there is suspect,
    not a genuine quiet day. Prefer the usage counter; fall back to counting
    server_tool_use blocks if the SDK shape differs.
    """
    usage = getattr(response, "usage", None)
    server = getattr(usage, "server_tool_use", None)
    requests = getattr(server, "web_search_requests", None)
    if requests is not None:
        return requests
    return sum(1 for b in response.content if getattr(b, "type", None) == "server_tool_use")


def _search_queries(response) -> list[str]:
    """The actual query strings the model issued to the server-side web_search
    tool, in order. web_search chooses these itself (there's no client-side
    query), so they live on the `server_tool_use` blocks' `input.query` and are
    otherwise unobservable — the single clearest signal for whether discovery is
    spending its searches on genuinely distinct angles or near-duplicate queries.
    Tolerant of the SDK's block shape (input may be a dict or an attribute).
    """
    queries: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) != "server_tool_use":
            continue
        data = getattr(block, "input", None)
        query = data.get("query") if isinstance(data, dict) else getattr(data, "query", None)
        if query:
            queries.append(query)
    return queries


class WebSearchSource:
    """Discovers postings via Claude's web_search tool, matched against the
    candidate's profile intent rather than a fixed keyword query.

    profile_intent: a natural-language description of what the candidate
        wants and the kinds of roles they pursue (typically their identity
        statement plus the titles of jobs they've previously applied to).
    location_desc: a human-readable statement of the location requirements
        (e.g. "in Switzerland, specifically the cantons of ...").
    """

    name = "web_search"

    def __init__(
        self,
        client: anthropic.Anthropic,
        profile_intent: str,
        location_desc: str,
        max_searches: int = _DEFAULT_MAX_SEARCHES,
    ):
        self.client = client
        self.profile_intent = profile_intent
        self.location_desc = location_desc
        self.max_searches = max_searches

    def fetch(self) -> FetchResult:
        # Bounded single attempt: a stuck web_search fails after _TIMEOUT_SECONDS
        # instead of retrying for many minutes. The raised error propagates to
        # _fetch_all, which records the source as degraded so the run falls back
        # to company_pages rather than hanging.
        model = model_for(_MODEL_VAR)
        response = self.client.with_options(
            timeout=_TIMEOUT_SECONDS, max_retries=0
        ).messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            tools=[
                {
                    "type": web_search_tool_type(model),
                    "name": "web_search",
                    "max_uses": self.max_searches,
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _PROMPT_TEMPLATE.format(
                        profile_intent=self.profile_intent,
                        location_desc=self.location_desc,
                    ),
                }
            ],
        )

        searches = _search_count(response)
        # Capture the actual query strings the model issued (server-side, so
        # otherwise unobservable) for the per-run observability artifact.
        queries = _search_queries(response)
        for q in queries:
            logger.info("web_search query: %s", q)

        text_blocks = [block.text for block in response.content if block.type == "text"]
        if not text_blocks:
            logger.warning("web_search discovery returned no text content (%d searches)", searches)
            return FetchResult(
                self.name, [], ok=False, detail=f"{searches} searches, no text content returned",
                meta={"queries": queries, "searches": searches},
            )

        try:
            postings = _parse_postings(text_blocks)
        except (ValueError, ValidationError) as exc:
            # Log a snippet of the joined output so a parse failure on a real
            # run is diagnosable without re-spending on another live search.
            snippet = "\n".join(text_blocks)[:600]
            logger.warning("Failed to parse web_search discovery output: %s; got: %r", exc, snippet)
            return FetchResult(
                self.name, [], ok=False, detail=f"{searches} searches, unparseable output",
                meta={"queries": queries, "searches": searches},
            )

        raw = [
            RawPosting(
                source="web_search",
                url=p.url,
                title=p.title,
                company=p.company,
                description=p.description,
                location=p.location,
                raw=p.model_dump(),
            )
            for p in postings
        ]

        # Log what was discovered (company, title, url) so the link mix is
        # auditable from the run output — useful for spotting aggregator/stale
        # links before they're pruned.
        for p in raw:
            logger.info("web_search discovered: %s — %s — %s", p.company, p.title, p.url)

        # Drop known aggregator/mirror links in code before the liveness check.
        # They return 200 (or bot-block us) even when the role closed, so liveness
        # can't tell they're stale — but a stale aggregator link is exactly the
        # kind that reached the report as a dead "best match". The prompt's
        # anti-aggregator steer is soft; this is the hard backstop.
        direct = [p for p in raw if not _is_aggregator(p.url)]
        aggregators = [p for p in raw if _is_aggregator(p.url)]
        for p in aggregators:
            logger.info("web_search dropped aggregator link: %s — %s", p.title, p.url)

        # Drop bare careers-board roots (no path, no query). These aren't real
        # permalinks: they defeat URL-keyed de-duplication (the same role's real
        # permalink from a direct source hashes to a different id) and would ship
        # a homepage link as the "apply" URL. Backstop for the model returning a
        # lossy link despite the prompt's "most direct URL" steer.
        pathed = [p for p in direct if not _is_pathless(p.url)]
        pathless = [p for p in direct if _is_pathless(p.url)]
        for p in pathless:
            logger.info("web_search dropped pathless link: %s — %s", p.title, p.url)
        direct = pathed

        # Liveness check: drop links that are definitively gone (404/410) before
        # they reach scoring. Catches dead *direct* postings (the stale-Swisscom
        # failure mode). Kept links carry whether the check actually confirmed
        # them — an unverified one survives, but is counted and reported apart
        # from a confirmed one rather than folded into a single "live" number.
        kept, dead = filter_live(direct)
        for p in dead:
            logger.info("web_search dropped dead link: %s — %s", p.title, p.url)
        confirmed = [p for p in kept if p.liveness == LIVENESS_CONFIRMED]
        unverified = [p for p in kept if p.liveness == LIVENESS_UNVERIFIED]
        for p in unverified:
            logger.info("web_search kept unverified link: %s — %s", p.title, p.url)

        # An empty result is only trustworthy if the model actually searched.
        # 0 searches AND 0 postings means it declined to look at all — flag that
        # as degraded so it isn't reported as a genuine quiet day.
        ok = searches > 0 or bool(raw)
        detail = f"{searches} searches, {len(raw)} found, {len(confirmed)} live"
        if unverified:
            detail += f", {len(unverified)} unverified"
        if aggregators:
            detail += f", {len(aggregators)} aggregator dropped"
        if pathless:
            detail += f", {len(pathless)} pathless dropped"
        if not ok:
            detail += " (model did not search)"
        # Structured discovery funnel for the per-run observability artifact:
        # the queries plus how many leads each pruning stage removed.
        meta = {
            "queries": queries,
            "searches": searches,
            "found": len(raw),
            "live": len(confirmed),
            "unverified": len(unverified),
            "aggregators_dropped": len(aggregators),
            "pathless_dropped": len(pathless),
            "dead_dropped": len(dead),
        }
        return FetchResult(self.name, kept, ok=ok, detail=detail, meta=meta)

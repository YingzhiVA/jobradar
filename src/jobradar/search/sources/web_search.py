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
from urllib.parse import parse_qsl, urlencode, urlsplit

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
    "glassdoor.de",
    "glassdoor.sg",
    "glassdoor.co.uk",
    "linkedin.com",
    "levels.fyi",
    "monster.com",
    "monster.ch",
    "stepstone.com",
    "stepstone.de",
    "xing.com",
    # Seen in web_search output up to 2026-10-02, mostly as search-result pages
    # (/jobs/<keyword>/in-switzerland) rather than a posting.
    "builtin.com",
    "dice.com",
    "efinancialcareers.com",
    "efinancialcareers.ch",
    "f6s.com",
    "freehire.me",
    "frontaliereticino.ch",
    "jobleads.com",
    "jobmaps.ch",
    "jobsinforex.com",
    "meetfrank.com",
    "remoterocketship.com",
    "wearedevelopers.com",
    "weloveproduct.co",
)


def _is_aggregator(url: str) -> bool:
    """True if the URL's host is (or is a subdomain of) a known aggregator/mirror."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


# Path segments that name a careers section rather than any one role, in the
# languages Swiss employers publish in. A URL whose path is made only of these
# (plus locale codes and file extensions) is a landing or listing page.
_LISTING_SEGMENTS = frozenset({
    # English
    "career", "careers", "job", "jobs", "jobs-and-careers", "careers-and-jobs",
    "vacancy", "vacancies", "opening", "openings", "current-openings",
    "open-positions", "open-roles", "positions", "roles", "opportunities",
    "join", "join-us", "joinus", "work-with-us", "work-for-us", "hiring",
    "we-are-hiring", "all-jobs", "job-search", "search", "search-results",
    "about", "about-us", "company", "team", "people", "life", "index", "home",
    # German
    "karriere", "karrieren", "stellen", "offene-stellen", "stellenangebote",
    "stellenmarkt", "jobsuche", "offene-positionen", "arbeiten-bei-uns",
    "unternehmen", "ueber-uns",
    # French and Italian
    "carriere", "carrieres", "carrière", "carrières", "emploi", "emplois",
    "offres", "offres-d-emploi", "postes", "postes-vacants", "rejoignez-nous",
    "lavora-con-noi", "lavoro", "carriera", "posizioni-aperte",
})

# en, de, fr-ch, en_US: language prefixes that say nothing about the role.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$")

# Query keys that pick out one posting (?gh_jid=123, ?jobId=9, ?id=4), as
# opposed to ones that filter or page a listing (?lang=en, ?page=2,
# ?department=product). Matched in full on the lowercased key.
_POSTING_QUERY_KEY = re.compile(
    r"id|jid|gh_jid|job|"
    r"(?:job|position|posting|req|requisition|vacancy|opening)[_-]?id"
)

# Tracking parameters that differ between a search result and the same link as
# the model copies it, and say nothing about which page it is.
_TRACKING_QUERY_PREFIXES = ("utm_", "gclid", "fbclid", "mc_", "_hs", "ref", "src", "source")


def _meaningful_query(query: str) -> list[tuple[str, str]]:
    """The query's parameters, tracking ones removed."""
    return [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_QUERY_PREFIXES)
    ]


# Hosted ATS boards, where the first path segment is the employer's board and a
# posting needs more after it: jobs.lever.co/acme is the board,
# jobs.lever.co/acme/<uuid> a role. Value is the fewest segments a posting has.
_ATS_POSTING_DEPTH = {
    "jobs.lever.co": 2,
    "jobs.eu.lever.co": 2,
    "jobs.ashbyhq.com": 2,
    "boards.greenhouse.io": 3,
    "job-boards.greenhouse.io": 3,
    "job-boards.eu.greenhouse.io": 3,
    "apply.workable.com": 3,
    "careers.smartrecruiters.com": 2,
    "jobs.smartrecruiters.com": 2,
}


def _query_names_posting(query: str) -> bool:
    """True if the query string carries a key that identifies one posting."""
    return any(_POSTING_QUERY_KEY.fullmatch(k.lower()) for k, _ in _meaningful_query(query))


def _is_listing_page(url: str) -> bool:
    """True if the URL is a careers landing or listing page rather than the page
    of one posting: a bare domain root, a path made only of generic careers
    words (https://www.liip.ch/jobs, https://www.frontify.com/en/careers), or a
    hosted ATS board with no posting under it (https://jobs.lever.co/acme).

    Such a link is never a permalink, and it is how a made-up role gets in.
    Every link in the 2026-10-02 report was one (Liip /jobs, Jua.ai, Frontify
    and PriceHubble /careers), all four titles were checked by hand, and none
    of the roles existed: the model had read that a company hires, named a
    plausible role, and handed back the careers page as its link. The page
    answers 200, so the liveness check called each one confirmed. A landing
    page also hashes to a different dedup id than the same role's real
    permalink from a direct source (the ETH AI Center repeat on 2026-07-09).

    A query key that names a posting keeps the URL (some ATSs put the job id
    there, e.g. /careers?gh_jid=123); one that only filters or pages a listing
    does not.
    """
    parts = urlsplit(url)
    if parts.query and _query_names_posting(parts.query):
        return False
    segments = [s for s in parts.path.lower().split("/") if s]
    host = (parts.hostname or "").lower()
    depth = _ATS_POSTING_DEPTH.get(host)
    if depth is not None:
        return len(segments) < depth
    for segment in segments:
        stem = segment.rsplit(".", 1)[0] if "." in segment else segment
        if stem not in _LISTING_SEGMENTS and not _LOCALE_SEGMENT.match(stem):
            return False
    return True


def _url_key(url: str) -> str:
    """A comparable form of a URL for matching the model's links against the
    search results: scheme, leading www., trailing slash, fragment and tracking
    parameters dropped, host lowercased, remaining query sorted.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    query = sorted(_meaningful_query(parts.query))
    return host + path + ("?" + urlencode(query) if query else "")


def _result_urls(response) -> set[str]:
    """Keys (see _url_key) of every URL the web_search tool actually returned
    this run: each search result, plus each citation on the answer text.

    These are the only links the model could have seen. web_search hands it
    result pages, not the open web, so a posting URL outside this set came from
    its own memory or was assembled by hand, and neither is a link to trust.
    Tolerant of the SDK's block shapes; an errored search contributes nothing.
    """
    keys: set[str] = set()
    for block in response.content:
        kind = getattr(block, "type", None)
        if kind == "web_search_tool_result":
            content = getattr(block, "content", None)
            for result in content if isinstance(content, list) else ():
                url = getattr(result, "url", None)
                if url:
                    keys.add(_url_key(url))
        elif kind == "text":
            for citation in getattr(block, "citations", None) or ():
                url = getattr(citation, "url", None)
                if url:
                    keys.add(_url_key(url))
    return keys

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

On LINKS, every posting must be a real, specific job opening you saw in your \
search results. This rule is strict, unlike the fit guidance above:
- The url must be the page of THAT ONE posting: the employer's own posting \
page or the direct ATS posting (Greenhouse, Lever, Ashby, Workday, \
SmartRecruiters, Personio, Workable, etc.), e.g. \
https://jobs.lever.co/acme/1b2c3d4e or https://www.acme.ch/jobs/senior-product-manager-ai.
- NEVER return a careers landing page, a job listing or search page, or a \
company homepage (e.g. https://www.acme.ch/careers, https://www.acme.ch/jobs, \
https://jobs.lever.co/acme). Code drops these, so returning one wastes the \
lead.
- Copy the url exactly as it appears in a search result. Never build, guess \
or complete a url, and never give one from memory: code checks every url \
against the search results and drops any that isn't among them.
- Use the title exactly as the posting states it. Do not name a role a \
company "probably" has because it hires in that area, and do not turn a \
company's general hiring page into a role. If you can't find the posting \
page itself, leave the role out.
- Write the description only from what the search result says about that \
posting.
- A search aimed at posting pages finds deep links far more often than a \
generic one, e.g. a query naming an ATS host (jobs.lever.co, \
boards.greenhouse.io, jobs.ashbyhq.com, jobs.personio.de, apply.workable.com) \
alongside the role and place.
- Avoid third-party aggregators and cached snapshots (e.g. levels.fyi, \
Glassdoor, LinkedIn job mirrors, jobs.ch, generic job-board search pages); \
they often serve listings that closed months ago, and code drops them. Favor \
roles that look recently posted.

Respond with ONLY a JSON object (no prose, no markdown fences) matching this shape:
{{"postings": [{{"title": str, "company": str, "url": str, "location": str | null, \
"description": str}}]}}

Return {{"postings": []}} if your searches found no posting pages for \
plausibly-fitting roles. An empty list is a valid answer; a guessed posting \
is not.
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

        # Drop careers landing and listing pages: bare roots, /careers, /jobs,
        # an ATS board with no posting under it. Not one of these is a permalink,
        # and a landing page is how a made-up role gets in: the model names a
        # role the company might have, attaches the careers page, and the page
        # answers 200 (every match in the 2026-10-02 report). Backstop for the
        # prompt's deep-link rule, which the model does not always follow.
        listing = [p for p in direct if _is_listing_page(p.url)]
        direct = [p for p in direct if not _is_listing_page(p.url)]
        for p in listing:
            logger.info("web_search dropped listing-page link: %s — %s", p.title, p.url)

        # Drop links the search never returned. web_search shows the model
        # result pages, not the open web, so a posting URL outside the results
        # was recalled from training or assembled by hand: a guess, whether at a
        # role that closed long ago or at one that never existed. Skipped, with a
        # warning, when the response carries no result URLs at all, so a change
        # in the SDK's block shape can't silently empty every run.
        grounded_urls = _result_urls(response)
        ungrounded: list[RawPosting] = []
        if grounded_urls:
            ungrounded = [p for p in direct if _url_key(p.url) not in grounded_urls]
            direct = [p for p in direct if _url_key(p.url) in grounded_urls]
            for p in ungrounded:
                logger.info("web_search dropped link not in search results: %s — %s", p.title, p.url)
        elif direct:
            logger.warning(
                "web_search response carried no result URLs (%d searches); "
                "could not check %d links against the search results",
                searches,
                len(direct),
            )

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
        if listing:
            detail += f", {len(listing)} listing page dropped"
        if ungrounded:
            detail += f", {len(ungrounded)} not in results dropped"
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
            "listing_dropped": len(listing),
            "ungrounded_dropped": len(ungrounded),
            "grounding_checked": bool(grounded_urls),
            "dead_dropped": len(dead),
        }
        return FetchResult(self.name, kept, ok=ok, detail=detail, meta=meta)

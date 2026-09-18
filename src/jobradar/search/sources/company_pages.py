"""Fetch postings directly from company career pages via their ATS's public
job-board JSON API. These endpoints are meant to be consumed programmatically
(they back the "jobs" widget on the company's own careers site), so this is
not scraping in any ToS-sensitive sense.

Each ATS has a slightly different response shape; `_fetch_*` functions below
normalize the fields we care about and stash everything else in `raw` so
later pipeline stages can dig deeper if needed. Field names are based on each
platform's public documentation as of this writing — if a company's board
returns something unexpected, fetch() skips that one company (logging a
warning) rather than failing the whole run.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import httpx

from .base import FetchResult, RawPosting, parse_workplace_type, strip_html

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0

# Share of configured companies that has to fail before the whole source is
# called degraded (which marks the run incomplete in the report and email).
# Set below the level where a result stops being a trustworthy picture of the
# market, but above ordinary board flakiness.
_DEGRADED_SKIP_FRACTION = 0.25


def _fetch_greenhouse(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = client.get(url)
    resp.raise_for_status()
    data = resp.json()
    postings = []
    for job in data.get("jobs", []):
        postings.append(
            RawPosting(
                source="greenhouse",
                url=job.get("absolute_url", ""),
                title=job.get("title", ""),
                company=company_name,
                description=strip_html(job.get("content", "")),
                location=(job.get("location") or {}).get("name"),
                raw=job,
            )
        )
    return postings


def _fetch_lever(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = client.get(url)
    resp.raise_for_status()
    data = resp.json()
    postings = []
    for job in data:
        description = job.get("descriptionPlain") or strip_html(job.get("description", ""))
        postings.append(
            RawPosting(
                source="lever",
                url=job.get("hostedUrl", ""),
                title=job.get("text", ""),
                company=company_name,
                description=description,
                location=(job.get("categories") or {}).get("location"),
                remote=parse_workplace_type(job.get("workplaceType")),
                raw=job,
            )
        )
    return postings


def _fetch_ashby(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = client.get(url)
    resp.raise_for_status()
    data = resp.json()
    postings = []
    for job in data.get("jobs", []):
        description = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml", ""))
        postings.append(
            RawPosting(
                source="ashby",
                url=job.get("jobUrl") or job.get("applyUrl", ""),
                title=job.get("title", ""),
                company=company_name,
                description=description,
                location=job.get("location"),
                remote=job.get("isRemote") if isinstance(job.get("isRemote"), bool) else None,
                raw=job,
            )
        )
    return postings


def _fetch_smartrecruiters(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # The list endpoint is paginated (limit <= 100) and carries only metadata;
    # the per-posting detail endpoint carries the description (jobAd.sections)
    # and the clean postingUrl. So this is N+1 calls per company — fine for the
    # small boards we target; revisit if we add a company with a huge board.
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    postings: list[RawPosting] = []
    offset = 0
    while True:
        resp = client.get(base, params={"limit": 100, "offset": offset})
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        for item in content:
            posting_id = item.get("id")
            if not posting_id:
                continue
            detail = client.get(f"{base}/{posting_id}")
            detail.raise_for_status()
            job = detail.json()
            sections = (job.get("jobAd") or {}).get("sections") or {}
            description = "\n\n".join(
                strip_html((sections.get(key) or {}).get("text", ""))
                for key in ("jobDescription", "qualifications", "additionalInformation")
                if (sections.get(key) or {}).get("text")
            )
            postings.append(
                RawPosting(
                    source="smartrecruiters",
                    url=job.get("postingUrl") or item.get("postingUrl", ""),
                    title=item.get("name", ""),
                    company=company_name,
                    description=description,
                    location=(item.get("location") or {}).get("fullLocation"),
                    raw=job,
                )
            )
        offset += len(content)
        # Stop when we've collected everything or a page came back empty.
        if not content or offset >= data.get("totalFound", 0):
            break
    return postings


def _fetch_personio(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # Personio exposes a public XML job feed at {slug}.jobs.personio.de/xml
    # (root <workzag-jobs>, one <position> per role). The feed has no apply URL,
    # so it's constructed as /job/{id}. Description lives across one or more
    # <jobDescription><value> HTML blocks.
    url = f"https://{slug}.jobs.personio.de/xml"
    resp = client.get(url)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    postings = []
    for pos in root.findall(".//position"):
        posting_id = (pos.findtext("id") or "").strip()
        if not posting_id:
            continue
        sections = []
        descriptions = pos.find("jobDescriptions")
        if descriptions is not None:
            for desc in descriptions.findall("jobDescription"):
                value = desc.findtext("value") or ""
                if value:
                    sections.append(strip_html(value))
        postings.append(
            RawPosting(
                source="personio",
                url=f"https://{slug}.jobs.personio.de/job/{posting_id}",
                title=(pos.findtext("name") or "").strip(),
                company=company_name,
                description="\n\n".join(sections),
                location=(pos.findtext("office") or "").strip() or None,
                raw={
                    "id": posting_id,
                    "office": pos.findtext("office"),
                    "department": pos.findtext("department"),
                    "employmentType": pos.findtext("employmentType"),
                    "schedule": pos.findtext("schedule"),
                },
            )
        )
    return postings


def _fetch_recruitee(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # Recruitee's public board API returns every offer inline in one call
    # (no pagination/detail round-trips), with the full location string and
    # description+requirements HTML.
    url = f"https://{slug}.recruitee.com/api/offers/"
    resp = client.get(url)
    resp.raise_for_status()
    postings = []
    for offer in resp.json().get("offers", []):
        if offer.get("status") != "published":
            continue  # skip drafts/closed/internal
        description = "\n\n".join(
            strip_html(offer.get(field) or "") for field in ("description", "requirements")
        ).strip()
        postings.append(
            RawPosting(
                source="recruitee",
                url=offer.get("careers_url", ""),
                title=offer.get("title", ""),
                company=company_name,
                description=description,
                location=offer.get("location") or offer.get("city"),
                remote=offer.get("remote") if isinstance(offer.get("remote"), bool) else None,
                raw={
                    "id": offer.get("id"),
                    "department": offer.get("department"),
                    "employment_type_code": offer.get("employment_type_code"),
                    "remote": offer.get("remote"),
                },
            )
        )
    return postings


def _fetch_workable(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # Workable's public embed-widget API returns all jobs inline, with the full
    # HTML description when details=true. (The endpoint answers 200 with jobs=[]
    # for any slug, so non-accounts simply yield nothing.)
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    resp = client.get(url)
    resp.raise_for_status()
    postings = []
    for job in resp.json().get("jobs", []):
        location = ", ".join(
            p for p in (job.get("city"), job.get("state"), job.get("country")) if p
        )
        if not location and job.get("telecommuting"):
            location = "Remote"
        postings.append(
            RawPosting(
                source="workable",
                url=job.get("url") or job.get("shortlink", ""),
                title=job.get("title", ""),
                company=company_name,
                description=strip_html(job.get("description", "")),
                location=location or None,
                raw={
                    "shortcode": job.get("shortcode"),
                    "department": job.get("department"),
                    "employment_type": job.get("employment_type"),
                    "telecommuting": job.get("telecommuting"),
                },
            )
        )
    return postings


def _teamtailor_location(jobposting: dict) -> str | None:
    # schema.org JobPosting jobLocation -> address. addressCountry is a code
    # ("CH"), so use locality + region (human-readable) for the location filter.
    locations = jobposting.get("jobLocation") or []
    if not locations:
        return None
    address = (locations[0] or {}).get("address") or {}
    parts = [address.get("addressLocality"), address.get("addressRegion")]
    return ", ".join(p for p in parts if p) or None


def _fetch_teamtailor(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # Teamtailor's per-company public feed (no auth) is JSON Feed format: each
    # item has title/url/content_html plus a _jobposting schema.org JobPosting
    # with structured location. (The official REST API needs a token; this feed
    # doesn't.)
    url = f"https://{slug}.teamtailor.com/jobs.json"
    resp = client.get(url)
    resp.raise_for_status()
    postings = []
    for item in resp.json().get("items", []):
        jobposting = item.get("_jobposting") or {}
        description = strip_html(item.get("content_html") or jobposting.get("description") or "")
        postings.append(
            RawPosting(
                source="teamtailor",
                url=item.get("url", ""),
                title=item.get("title") or jobposting.get("title", ""),
                company=company_name,
                description=description,
                location=_teamtailor_location(jobposting),
                raw={"id": item.get("id"), "date_published": item.get("date_published")},
            )
        )
    return postings


# Workday boards can be global and large (Novartis lists ~1000 postings
# worldwide, Johnson & Johnson ~1750), and the description only comes from a
# per-posting detail call — so fetching every posting would mean ~1750 HTTP
# round-trips per run. Multinational boards expose a server-side location facet
# carrying the country, so we pre-filter to these countries *before* any detail
# calls. This scopes the connector to Swiss postings to match the rest of the
# tool (constraints.yaml filters by Swiss canton, with the country left
# implicit); broaden this set if you ever target other countries. Matched
# case-insensitively against the facet's country descriptors.
_WORKDAY_TARGET_COUNTRIES = {"switzerland"}

# The Workday CXS country-facet parameter, whose descriptors are bare country
# names ("Switzerland"). Two spellings are in the wild: the standard
# `locationCountry` (Novartis) and `Location_Country` on tenants that built the
# facet themselves (Julius Baer, Takeda). Same shape, same semantics — read
# either. Before the second spelling was recognised, Julius Baer's 153-posting
# board was fetched whole (67 Swiss) and Takeda's was scoped off its `locations`
# facet instead, which reads only the country-qualified descriptors ("Zurich,
# Switzerland") and silently missed the site-coded ones ("CHE - Neuchatel"):
# 17 of 26 Swiss postings.
_WORKDAY_COUNTRY_FACETS = ("locationCountry", "Location_Country")

# The first level of Workday's location hierarchy. NVIDIA fills it with bare
# country names ("Switzerland") and has no country facet of either spelling
# above, while its `locations` descriptors lead with the country ("Switzerland,
# Zurich") instead of ending on it — so without this its 2000-posting board
# read as unscopable. Nothing guarantees the level holds countries (a tenant
# may put regions there), so unlike the facets above it is used only when a
# descriptor actually names a target country; otherwise the board falls through
# to the other checks rather than being silently skipped as "no Swiss postings".
_WORKDAY_HIERARCHY_COUNTRY_FACET = "locationHierarchy1"

# The other shape: a flat facet with one value per location and no country
# dimension of its own. A multinational that uses it (Johnson & Johnson)
# country-qualifies each descriptor — "Zug, Switzerland" — so the country is
# still readable; a single-country employer (Swisscom) lists bare cities
# ("Bern"), which is how the two are told apart. A third kind (Roche) is
# multinational but lists bare, mixed-granularity descriptors — cities, regions
# and countries in one flat list — so its country is unreadable at any size;
# those boards need the slug's explicit location list. Unknown facet parameters
# are no help there: Workday silently IGNORES an appliedFacets key its board
# doesn't have (no error, full unfiltered board back), so a country facet can't
# be smuggled onto a board that doesn't publish one.
# See _workday_location_facets.
_WORKDAY_LOCATIONS_FACET = "locations"

# Workday's job-search API caps `limit` at 20 per page.
_WORKDAY_PAGE_SIZE = 20

# Ceiling on a board we couldn't location-scope at all. Fetching one unscoped is
# fine for a small single-country employer (Swisscom, ~80 postings) but ruinous
# for a global board whose facets we misread: every posting costs a detail call
# and then a normalize pass. Above this, the company is skipped loudly instead —
# the facet handling above needs extending for that board, and a silent
# hundreds-of-requests run is the worse failure.
_WORKDAY_MAX_UNSCOPED_POSTINGS = 400


def _workday_names_target_country(descriptor: str | None) -> bool:
    """`locationCountry` descriptors only: the whole string is a country name."""
    if not descriptor:
        return False
    return descriptor.strip().lower() in _WORKDAY_TARGET_COUNTRIES


def _workday_location_in_target_country(descriptor: str | None) -> bool:
    """`locations` descriptors only: the country is an explicit suffix.

    The comma is load-bearing, not cosmetic. A `locations` facet is a flat list
    with no country dimension of its own, so a country name is only trustworthy
    evidence of one when it *qualifies* a place ("Zug, Switzerland"). Roche's
    board mixes granularities in this one list — cities ("Basel", "Rotkreuz"),
    regions ("Hesse"), and countries ("France", "Switzerland") — where bare
    "Switzerland" is one location among 208 carrying a single posting, not the
    board's Swiss dimension. Matching it would have scoped a 1172-posting board
    to that one req and silently dropped the other 125 Swiss ones (a board still
    answering 1/day reads as healthy to company_health). Requiring the suffix
    makes such a board fall through to the unscopable-size guard, which fails
    loudly, or to an explicit `locations=` allowlist in its slug.
    """
    if not descriptor or "," not in descriptor:
        return False
    return descriptor.rsplit(",", 1)[-1].strip().lower() in _WORKDAY_TARGET_COUNTRIES


def _workday_location_facets(
    facets: list[dict], allowed_locations: list[str] | None = None
) -> dict[str, list[str]] | None:
    """Decide how to location-scope a Workday board from its facet tree.

    Returns the `appliedFacets` dict to send (filtering to target countries), or
    `None` meaning "no readable country dimension — fetch the whole board".
    Facet values are nested (e.g. locationMainGroup -> locationCountry ->
    [{descriptor: "Switzerland", id: ...}, ...]), so the tree is flattened by
    facet parameter first.

    - `allowed_locations` given (Roche): return `{"locations": [ids]}` for the
      `locations` descriptors named there, whatever country they're in. This is
      the escape hatch for a board whose location facet has no country dimension
      to read; the descriptors come from the slug's 4th segment.
    - Board HAS a country facet — `locationCountry` (Novartis) or the
      `Location_Country` spelling (Julius Baer, Takeda): return `{<that facet>:
      [ids]}` for matching countries — an empty id list if none match (caller
      treats that as "no in-scope postings"). Checked before `locations`: a
      board with both (Takeda) has the complete Swiss set only on the country
      facet.
    - Board has neither, but its `locationHierarchy1` level names a target
      country (NVIDIA): return `{"locationHierarchy1": [ids]}`. Never an empty
      list: a level naming no target country may not hold countries at all.
    - Board has a flat `locations` facet with country-qualified descriptors
      (Johnson & Johnson): return `{"locations": [ids]}` for the locations in a
      target country. This is what keeps a 1750-posting global board down to the
      ~60 Swiss ones before any detail call.
    - Neither matched (single-country employer like Swisscom, which lists bare
      cities): return `None` so the caller fetches everything; the board is
      already in one country and the downstream canton filter trims it.
    """
    by_param: dict[str, list[dict]] = {}

    def visit(node: dict) -> None:
        param = node.get("facetParameter")
        children = node.get("values", [])
        if param:
            by_param.setdefault(param, []).extend(children)
        for child in children:
            if child.get("values"):
                visit(child)

    for facet in facets:
        visit(facet)

    def matching_ids(param: str, matches) -> list[str]:
        return [
            value["id"]
            for value in by_param.get(param, [])
            if value.get("id") and matches(value.get("descriptor"))
        ]

    if allowed_locations:
        by_descriptor = {
            (value.get("descriptor") or "").strip().lower(): value["id"]
            for value in by_param.get(_WORKDAY_LOCATIONS_FACET, [])
            if value.get("id")
        }
        wanted = {name.strip().lower(): name.strip() for name in allowed_locations}
        # A configured name that matches nothing is drift, not a typo to ignore:
        # the board renamed or retired that site, and the run would quietly cover
        # less than the config claims. Say so — an empty result then also skips
        # the company, which company_health picks up as a board gone dry.
        for key in sorted(set(wanted) - set(by_descriptor)):
            logger.warning(
                "Workday location %r is not on this board's location facet", wanted[key]
            )
        return {
            _WORKDAY_LOCATIONS_FACET: [
                by_descriptor[key] for key in sorted(set(wanted) & set(by_descriptor))
            ]
        }
    for country_facet in _WORKDAY_COUNTRY_FACETS:
        if country_facet in by_param:
            return {country_facet: matching_ids(country_facet, _workday_names_target_country)}
    hierarchy_ids = matching_ids(_WORKDAY_HIERARCHY_COUNTRY_FACET, _workday_names_target_country)
    if hierarchy_ids:
        return {_WORKDAY_HIERARCHY_COUNTRY_FACET: hierarchy_ids}
    location_ids = matching_ids(_WORKDAY_LOCATIONS_FACET, _workday_location_in_target_country)
    if location_ids:
        return {_WORKDAY_LOCATIONS_FACET: location_ids}
    return None


def _workday_location(info: dict) -> str | None:
    """Every location a posting is open in, as one string.

    A multi-location req names only its primary in `location` and puts the rest
    in `additionalLocations`, while the list call collapses them to "5
    Locations" — so a role open in both Beerse and Zug reads as Belgium-only and
    the Swiss location filter drops it. Joining them keeps every location visible
    to filters.py and to normalize.py's canton resolution.
    """
    locations = [info.get("location"), *(info.get("additionalLocations") or [])]
    unique = dict.fromkeys(loc.strip() for loc in locations if loc and loc.strip())
    return "; ".join(unique) or None


def _fetch_workday(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # Workday needs three coordinates, not one token, so the slug is
    # "tenant:host:site", e.g. "novartis:wd3:Novartis_Careers". The host is the
    # datacenter segment (wd1/wd3/wd5/wd103) from the company's myworkdayjobs URL.
    # An optional 4th segment pipe-separates location-facet descriptors to scope
    # the board to ("...:roche-ext:Rotkreuz"), for boards whose location facet
    # carries no country to read — same idea as the Avature slug's scoping
    # segment. Names are matched against the facet, so use the board's exact
    # spelling.
    parts = slug.split(":")
    if len(parts) not in (3, 4) or not all(parts):
        raise ValueError(f"Workday slug must be 'tenant:host:site[:Loc1|Loc2]', got {slug!r}")
    tenant, host, site = parts[:3]
    allowed_locations = [name for name in parts[3].split("|") if name.strip()] if len(parts) == 4 else None
    base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"

    # One discovery call (no facets) to read the location facets, then paginate
    # the location-scoped result so the N+1 detail calls below only run for
    # in-scope postings.
    probe = client.post(base + "/jobs", json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    probe.raise_for_status()
    probe_data = probe.json()
    location_facets = _workday_location_facets(probe_data.get("facets", []), allowed_locations)
    if location_facets is not None and not any(location_facets.values()):
        # Nothing in scope: either the board has a country facet naming none of
        # our target countries, or none of the configured locations exist on it.
        # Return [] rather than fetch the whole board.
        scope = "|".join(allowed_locations) if allowed_locations else "/".join(_WORKDAY_TARGET_COUNTRIES)
        logger.info("Skipping %s (workday): no %s postings", company_name, scope)
        return []
    if location_facets is None:
        total = probe_data.get("total", 0)
        if total > _WORKDAY_MAX_UNSCOPED_POSTINGS:
            raise ValueError(
                f"board is unscopable ({total} postings, no readable location facet; "
                f"limit {_WORKDAY_MAX_UNSCOPED_POSTINGS})"
            )
    applied_facets = location_facets or {}  # None -> single-country board, fetch all

    postings: list[RawPosting] = []
    offset = 0
    total: int | None = None
    while True:
        resp = client.post(
            base + "/jobs",
            json={"appliedFacets": applied_facets, "limit": _WORKDAY_PAGE_SIZE, "offset": offset, "searchText": ""},
        )
        resp.raise_for_status()
        data = resp.json()
        # Workday reports `total` only on the first page (later pages say 0), and
        # an out-of-range offset wraps back to a full page rather than returning
        # empty — so latch the count from page one and stop strictly on it.
        if total is None:
            total = data.get("total", 0)
        page = data.get("jobPostings", [])
        if not page:
            break
        for job in page:
            external_path = job.get("externalPath")
            if not external_path:
                continue
            # A posting can close between the list call and its detail call
            # (Workday's search index lags de-listing): the detail then 403s/404s.
            # Skip that one posting rather than let it abort the whole company.
            try:
                detail = client.get(base + external_path)
                detail.raise_for_status()
            except httpx.HTTPError as exc:
                logger.info("Skipping closed/unavailable %s posting %s: %s", company_name, external_path, exc)
                continue
            info = detail.json().get("jobPostingInfo") or {}
            # canApply=False means the req is no longer open to applications even
            # though it's still indexed — not actionable, so drop it.
            if info.get("canApply") is False:
                logger.info("Skipping non-applyable %s posting %s", company_name, external_path)
                continue
            postings.append(
                RawPosting(
                    source="workday",
                    url=info.get("externalUrl") or "",
                    title=info.get("title") or job.get("title", ""),
                    company=company_name,
                    description=strip_html(info.get("jobDescription", "")),
                    location=_workday_location(info) or job.get("locationsText"),
                    raw={
                        "jobReqId": info.get("jobReqId"),
                        "timeType": info.get("timeType"),
                        "country": (info.get("country") or {}).get("descriptor"),
                    },
                )
            )
        offset += len(page)
        if offset >= total:
            break
    return postings


_JOIN_NEXT_DATA_RE = re.compile(r'__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def parse_join_company(page_html: str) -> dict | None:
    """Pulls the `company` object (id, name, domain, ...) out of a join.com
    careers page's embedded Next.js data blob.

    join.com's public jobs API is keyed by a numeric company id, but the
    careers page URL only exposes the domain slug, and there's no public
    lookup-by-domain endpoint — so both the connector (to resolve id -> jobs)
    and discovery's probe (to verify a candidate slug) read it out of this
    blob instead.
    """
    match = _JOIN_NEXT_DATA_RE.search(page_html)
    if not match:
        return None
    try:
        next_data = json.loads(match.group(1))
        return next_data["props"]["pageProps"]["initialState"]["company"]
    except (ValueError, KeyError, TypeError):
        return None


# join.com's public jobs API rejects any pageSize above 5 with HTTP 422
# ({"param":"pageSize","msg":"Invalid value"}) — verified 2026-07-17 by
# bisecting: 1-5 answer 200, 6+ are refused. It used to accept 50, and that
# stale value silently killed every join company (raise_for_status turned the
# 422 into a per-company skip). Discovery's join probe calls the same endpoint,
# so the cap lives here rather than in two places.
_JOIN_PAGE_SIZE = 5


def join_jobs_page(client: httpx.Client, company_id: int, page: int = 1) -> dict:
    """One page of a join.com company's public job list (raises on HTTP error)."""
    resp = client.get(
        f"https://join.com/api/public/companies/{company_id}/jobs",
        params={"page": page, "pageSize": _JOIN_PAGE_SIZE},
    )
    resp.raise_for_status()
    return resp.json()


def _bamboohr_location(job: dict) -> str | None:
    # A BambooHR posting carries location twice: `location` (free-text city/state
    # the employer typed) and `atsLocation` (BambooHR's structured
    # city/state/province/country). Either can be the populated one — some boards
    # fill only `location`, others only `atsLocation` — so merge them field by
    # field, preferring the structured atsLocation, and pick up the country that
    # only atsLocation carries.
    ats = job.get("atsLocation") or {}
    loc = job.get("location") or {}
    parts = [
        ats.get("city") or loc.get("city"),
        ats.get("state") or ats.get("province") or loc.get("state"),
        ats.get("country"),
    ]
    return ", ".join(p for p in parts if p) or None


def _fetch_bamboohr(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # BambooHR hosts each customer's board at {slug}.bamboohr.com. Its public
    # embed API lists every opening in one call (no pagination), but the
    # description only comes from the per-posting /detail endpoint — so this is
    # N+1 calls per company, like SmartRecruiters. A non-customer subdomain
    # 302-redirects to the BambooHR marketing site; httpx doesn't follow it, so
    # the list call is a 302 whose empty body then fails .json() — caught upstream
    # as a per-company skip.
    base = f"https://{slug}.bamboohr.com/careers"
    resp = client.get(f"{base}/list")
    resp.raise_for_status()
    postings = []
    for item in resp.json().get("result", []):
        posting_id = item.get("id")
        if not posting_id:
            continue
        detail = client.get(f"{base}/{posting_id}/detail")
        detail.raise_for_status()
        opening = (detail.json().get("result") or {}).get("jobOpening") or {}
        postings.append(
            RawPosting(
                source="bamboohr",
                # jobOpeningShareUrl is the public posting URL; fall back to the
                # deterministic /careers/{id} form it always mirrors.
                url=opening.get("jobOpeningShareUrl") or f"{base}/{posting_id}",
                title=item.get("jobOpeningName", ""),
                company=company_name,
                description=strip_html(opening.get("description", "")),
                location=_bamboohr_location(item),
                remote=item.get("isRemote") if isinstance(item.get("isRemote"), bool) else None,
                raw={
                    "id": posting_id,
                    "department": item.get("departmentLabel"),
                    "employmentStatus": item.get("employmentStatusLabel"),
                    "compensation": opening.get("compensation"),
                    "datePosted": opening.get("datePosted"),
                    "isRemote": item.get("isRemote"),
                },
            )
        )
    return postings


def _fetch_join(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    page = client.get(f"https://join.com/companies/{slug}")
    page.raise_for_status()
    company = parse_join_company(page.text)
    if company is None:
        raise ValueError(f"join.com: no company data found for slug {slug!r}")
    company_id = company["id"]

    postings: list[RawPosting] = []
    page_num = 1
    while True:
        data = join_jobs_page(client, company_id, page_num)
        for item in data.get("items", []):
            detail = client.get(f"https://join.com/api/public/jobs/{item['id']}")
            detail.raise_for_status()
            job = detail.json()
            location = ", ".join(
                p
                for p in ((item.get("city") or {}).get("cityName"), (item.get("country") or {}).get("name"))
                if p
            ) or None
            postings.append(
                RawPosting(
                    source="join",
                    url=f"https://join.com/companies/{slug}/{item.get('idParam', '')}",
                    title=item.get("title", ""),
                    company=company_name,
                    description=job.get("description", ""),
                    location=location,
                    remote=parse_workplace_type(item.get("workplaceType")),
                    raw={"id": item.get("id"), "workplaceType": item.get("workplaceType")},
                )
            )
        # pageCount is 0 for a board with no open postings at all.
        if page_num >= data.get("pagination", {}).get("pageCount", 0):
            break
        page_num += 1
    return postings


# Unlike the JSON APIs above, Avature and SuccessFactors career sites expose no
# public job-board API — but their pages are server-rendered HTML (no JS needed),
# and both platforms' markup is stable and structured enough to parse with a few
# regexes. Same ToS posture as eth.py: public postings on the company's own
# careers site, fetched at the pace of one small crawl per day. Sites answer
# httpx's default UA today; send a browser-ish UA anyway to stay off bot
# heuristics.
_HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) jobradar"
}

# --- Avature ("Careers Marketplace": Siemens, Deloitte CH, Siemens Healthineers) ---

_AVATURE_JOB_LINK_RE = re.compile(r'href="([^"#?]*/JobDetail/[^"#?]+)')
# Pagination links name their offset param after the site's record type
# ("folderOffset" on jobs.siemens.com, "jobOffset" on apply.deloitte.ch), so the
# prefix and the site's fixed page size are read off the page's own pagination
# links rather than hardcoded. (Overriding RecordsPerPage upward is ignored, and
# the RSS feed at SearchJobs/.../feed/ is capped at 20 items with no working
# offset at all — verified 2026-08-12 — which is why this parses HTML instead.)
_AVATURE_PAGINATION_RE = re.compile(r"([A-Za-z]+)RecordsPerPage=(\d+)&(?:amp;)?\1Offset=\d+")
_AVATURE_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
# A detail page splits its content over several article--details blocks: the
# first holds the labeled metadata fields, a later one the "Job description"
# prose — so join them all (verified on Siemens and Deloitte, 2026-08-12).
_AVATURE_ARTICLE_RE = re.compile(r'<article class="[^"]*article--details.*?</article>', re.S)
# The detail page's labeled metadata fields, e.g. "City" -> "Basel, Geneva,
# Zurich" (Deloitte) or "Location(s)" -> "Wallisellen - Zuerich - Switzerland"
# and "Organization" -> "Smart Infrastructure" (Siemens).
_AVATURE_FIELD_RE = re.compile(
    r"field__label[^>]*>\s*(.*?)\s*<.*?field__value[^>]*>(.*?)</div>", re.S
)


def _avature_fields(page_html: str) -> dict[str, str]:
    return {
        strip_html(label): strip_html(value)
        for label, value in _AVATURE_FIELD_RE.findall(page_html)
        if strip_html(label)
    }


def _avature_location(fields: dict[str, str]) -> str | None:
    for label, value in fields.items():
        if "location" in label.lower() or label.lower() == "city":
            return value or None
    return None


def _fetch_avature(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # Avature needs a host + portal path, plus an optional search path segment
    # that scopes the listing server-side — so the slug is
    # "host:portalPath:searchPathSegment", e.g.
    # "jobs.siemens.com:en_US/externaljobs:Switzerland||" (the trailing "||" is
    # Avature's term/facet separator). Leave the segment empty for a board
    # that's already single-country ("apply.deloitte.ch:CHCareers:").
    # The segment is a full-text search term, not a location facet: it narrows
    # a global board server-side, but a foreign posting that merely mentions
    # the term comes along too — the downstream location gate drops those
    # before they cost anything.
    parts = slug.split(":")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise ValueError(f"Avature slug must be 'host:portalPath:searchPathSegment', got {slug!r}")
    host, portal, search_path = parts
    search_url = f"https://{host}/{portal}/SearchJobs/"
    if search_path:
        search_url += f"{search_path}/"

    detail_urls: list[str] = []

    def collect(page_html: str) -> int:
        new = 0
        for href in _AVATURE_JOB_LINK_RE.findall(page_html):
            url = urljoin(f"https://{host}/", html.unescape(href))
            if url not in detail_urls:
                detail_urls.append(url)
                new += 1
        return new

    resp = client.get(search_url, headers=_HTML_HEADERS, follow_redirects=True)
    resp.raise_for_status()
    collect(resp.text)
    pagination = _AVATURE_PAGINATION_RE.search(resp.text)
    if pagination:
        prefix, page_size = pagination.group(1), int(pagination.group(2))
        offset = page_size
        while True:
            resp = client.get(
                search_url,
                params={f"{prefix}RecordsPerPage": page_size, f"{prefix}Offset": offset},
                headers=_HTML_HEADERS,
                follow_redirects=True,
            )
            resp.raise_for_status()
            # "No new links" doubles as the stop condition for a site that
            # ignores the offset param and serves page 1 forever.
            if collect(resp.text) == 0:
                break
            offset += page_size

    postings: list[RawPosting] = []
    for url in detail_urls:
        # A posting can close between the list call and its detail call; skip
        # that one posting rather than abort the whole company (same rationale
        # as the Workday fetcher).
        try:
            detail = client.get(url, headers=_HTML_HEADERS, follow_redirects=True)
            detail.raise_for_status()
        except httpx.HTTPError as exc:
            logger.info("Skipping unavailable %s posting %s: %s", company_name, url, exc)
            continue
        title_match = _AVATURE_OG_TITLE_RE.search(detail.text)
        articles = _AVATURE_ARTICLE_RE.findall(detail.text)
        fields = _avature_fields(detail.text)
        postings.append(
            RawPosting(
                source="avature",
                url=url,
                title=html.unescape(title_match.group(1)) if title_match else "",
                company=company_name,
                # Metadata fields (location, workload %, organization) lead,
                # prose follows — both useful context for scoring, so keep all.
                description="\n\n".join(strip_html(block) for block in articles).strip(),
                location=_avature_location(fields),
                raw=fields,
            )
        )
    return postings


# --- SAP SuccessFactors Career Site Builder (EY, Swiss Re) ---

# Baked-in server-side location filter, for the same reason as
# _WORKDAY_TARGET_COUNTRIES: these boards are global (EY lists thousands of
# postings worldwide) and the description costs a detail call per posting, so
# scope to Switzerland before any detail call. Broaden if you ever target
# other countries.
_SF_LOCATION_SEARCH = "Switzerland"

_SF_ROW_RE = re.compile(r'<tr class="data-row.*?</tr>', re.S)
_SF_ANCHOR_RE = re.compile(r'<a\s([^>]*class="jobTitle-link"[^>]*)>(.*?)</a>', re.S)
_SF_HREF_RE = re.compile(r'href="([^"]+)"')
_SF_LOCATION_RE = re.compile(r'class="jobLocation">(.*?)</span>', re.S)
# "Results 1 – 25 of 49": the board's own count of the location-filtered
# search, used to stop paging. Past the last page some boards (Sonova) serve
# a page of UNFILTERED postings rather than the last page again — new rows,
# so "nothing new" alone would keep going and pull foreign roles in.
_SF_TOTAL_RE = re.compile(r"of\s*<b>\s*(\d+)\s*</b>")


def _sf_parse_rows(page_html: str) -> list[tuple[str, str, str | None]]:
    """(href, title, location) per job row of a search-results page.

    Each row renders the title link twice (desktop table cell + phone card);
    taking the first anchor per row keeps one entry per job.
    """
    rows: list[tuple[str, str, str | None]] = []
    for row in _SF_ROW_RE.findall(page_html):
        anchor = _SF_ANCHOR_RE.search(row)
        if not anchor:
            continue
        href = _SF_HREF_RE.search(anchor.group(1))
        if not href:
            continue
        location_match = _SF_LOCATION_RE.search(row)
        location = strip_html(location_match.group(1)) if location_match else ""
        rows.append((html.unescape(href.group(1)), strip_html(anchor.group(2)), location or None))
    return rows


def _sf_description(page_html: str) -> str:
    """The <span class="jobdescription"> block, closed by tag balance.

    The block nests arbitrary styling spans, so the matching close can't be
    found with a non-greedy regex — count span opens/closes instead.
    """
    start = page_html.find('<span class="jobdescription"')
    if start == -1:
        return ""
    depth = 0
    for tag in re.finditer(r"<span\b|</span>", page_html[start:]):
        depth += 1 if tag.group(0) != "</span>" else -1
        if depth == 0:
            return strip_html(page_html[start : start + tag.end()])
    return strip_html(page_html[start:])


def _fetch_successfactors(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # The slug is the search page's host[/site] prefix — "careers.ey.com/ey"
    # (site mounted under a path) or "careers.swissre.com" (mounted at root).
    # Job hrefs are host-relative, so the host is also the join base for them.
    if not slug or "//" in slug:
        raise ValueError(f"SuccessFactors slug must be 'host[/site]', got {slug!r}")
    host = slug.split("/", 1)[0]
    seen: set[str] = set()
    postings: list[RawPosting] = []
    startrow = 0
    total: int | None = None
    while True:
        resp = client.get(
            f"https://{slug}/search/",
            params={"q": "", "locationsearch": _SF_LOCATION_SEARCH, "startrow": startrow},
            headers=_HTML_HEADERS,
            follow_redirects=True,
        )
        resp.raise_for_status()
        if total is None:
            total_match = _SF_TOTAL_RE.search(resp.text)
            total = int(total_match.group(1)) if total_match else None
        rows = [(h, t, loc) for h, t, loc in _sf_parse_rows(resp.text) if h not in seen]
        # An out-of-range startrow serves the last page again rather than an
        # empty one, so "nothing new" is the stop condition, not "no rows".
        if not rows:
            break
        if total is not None and startrow >= total:
            break  # past the board's own count: whatever this page holds is off-search
        for href, title, location in rows:
            seen.add(href)
            url = urljoin(f"https://{host}/", href)
            # Same per-posting skip as Avature/Workday: a posting that closed
            # since the list call 404s here.
            try:
                detail = client.get(url, headers=_HTML_HEADERS, follow_redirects=True)
                detail.raise_for_status()
            except httpx.HTTPError as exc:
                logger.info("Skipping unavailable %s posting %s: %s", company_name, url, exc)
                continue
            postings.append(
                RawPosting(
                    source="successfactors",
                    url=url,
                    title=title,
                    company=company_name,
                    description=_sf_description(detail.text),
                    location=location,
                    raw={},
                )
            )
        startrow += len(rows)
    return postings


# --- iCIMS career sites with the Jibe front-end (AXA) ---

# Jibe (iCIMS's career-site product) renders its listing from a JSON endpoint
# at /api/jobs on the careers host, which takes the same facets the page's
# filter sidebar offers — so this is a real job-board API, not a scrape, and
# unlike Avature/SuccessFactors the listing payload already carries the full
# description: no per-posting detail call. Verified on careers.axa.com,
# 2026-09-07.

# Server-side country facet, for the same reason as _SF_LOCATION_SEARCH: the
# board is global (AXA lists ~1500 postings worldwide) and only the Swiss ones
# are wanted. It's a facet on the posting's primary site, so a multi-site
# posting whose primary site is abroad but that also lists a Swiss office
# rides along (its full_location names every site; the location gate reads
# the Swiss one off it). Broaden if you ever target other countries.
_ICIMS_COUNTRY = "Switzerland"
# The endpoint rejects limit > 100 with a 422 (verified 2026-09-07); the
# page's own default is 10.
_ICIMS_PAGE_SIZE = 100


def _icims_location(data: dict) -> str | None:
    # full_location is "WINTERTHUR, Switzerland", or for a multi-site posting
    # "DUBLIN, Ireland; PARIS, France; ZURICH, Switzerland" — keep every site so
    # the downstream canton lookup finds the Swiss one. Cities come uppercased;
    # the canton lookup is case-insensitive, so they're passed through as-is.
    for key in ("full_location", "short_location"):
        if data.get(key):
            return data[key]
    city, country = data.get("city"), data.get("country")
    if city or country:
        return ", ".join(part for part in (city, country) if part)
    return None


def _fetch_icims(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # The slug is the careers host, e.g. "careers.axa.com" (jobs.axa.ch
    # redirects there). The JSON API lives at /api/jobs on it.
    if not slug or "/" in slug:
        raise ValueError(f"iCIMS slug must be the careers host, got {slug!r}")
    api_url = f"https://{slug}/api/jobs"
    seen: set[str] = set()
    postings: list[RawPosting] = []
    page = 1
    while True:
        resp = client.get(
            api_url,
            params={
                "page": page,
                "limit": _ICIMS_PAGE_SIZE,
                "country": _ICIMS_COUNTRY,
                "sortBy": "relevance",
                "descending": "false",
                "internal": "false",
            },
            headers=_HTML_HEADERS,
        )
        resp.raise_for_status()
        jobs = [job.get("data") or {} for job in resp.json().get("jobs", [])]
        new = 0
        for data in jobs:
            req_id = str(data.get("req_id") or data.get("slug") or "")
            if not req_id or req_id in seen:
                continue
            seen.add(req_id)
            new += 1
            # The site names its own canonical job URL in meta_data; fall back
            # to Jibe's conventional /jobs/<id> path, which the site redirects
            # to wherever it mounts job pages.
            url = (data.get("meta_data") or {}).get("canonical_url") or f"https://{slug}/jobs/{req_id}"
            postings.append(
                RawPosting(
                    source="icims",
                    url=url,
                    title=html.unescape(data.get("title") or ""),
                    company=company_name,
                    # Plain text on AXA's board, but strip anyway: entities and
                    # whitespace runs are cheap to clean and other tenants may
                    # send HTML.
                    description=strip_html(data.get("description") or ""),
                    location=_icims_location(data),
                    raw=data,
                )
            )
        # A page past the end serves the last page again on some tenants and
        # an empty list on others; "nothing new" covers both, and a short page
        # is the last one, which saves the extra call (each page is ~1.5 MB).
        if new == 0 or len(jobs) < _ICIMS_PAGE_SIZE:
            break
        page += 1
    return postings


# --- Infinite BrassRing Talent Gateway, "TGnewUI" (UBS) ---

# BrassRing (ex-IBM Kenexa) Talent Gateways are Angular apps, but the search
# page pre-renders its first page of results — facets, job summaries and the
# request schema the app posts — into a hidden <input id="preLoadJSON">, and
# the app pages through results with a JSON POST to Search/Ajax/MatchedJobs
# that is guarded only by the page's anti-forgery token (sent back as an "RFT"
# header, with the tg_rft cookie the client jar keeps). A job's detail page
# pre-renders the same way, with every labeled section of the posting in
# Jobdetails.JobDetailQuestions — the list call carries only the first
# section, so there is one detail call per posting. Same ToS posture as
# Avature/SuccessFactors: public postings on the company's own site, one
# small crawl a day. The pages are heavy (the search page is ~1.7 MB, a detail
# ~0.4 MB), which is the real cost here, not the request count. Verified on
# jobs.ubs.com, 2026-09-07.
# Only the attribute's start is matched: the JSON inside is HTML-escaped
# inconsistently (UBS's carries raw quotes in some string values), so its end
# can't be found by regex — the JSON decoder finds the object's end instead.
_BRASSRING_PRELOAD_RE = re.compile(r'id="preLoadJSON"[^>]*?\bvalue="')
_BRASSRING_RFT_RE = re.compile(r'name="__RequestVerificationToken"[^>]*\bvalue="([^"]+)"')
_BRASSRING_SESSION_RE = re.compile(r'id="CookieValue"[^>]*\bvalue="([^"]*)"')
# Ceiling for a board fetched without a facet scope in its slug — every posting
# is a ~0.4 MB detail page, so an unscoped 500-posting board is ~200 MB a day.
# Same idea as _WORKDAY_MAX_UNSCOPED_POSTINGS: fail loudly, don't crawl quietly.
_BRASSRING_MAX_UNSCOPED_POSTINGS = 100


def _brassring_preload(page_html: str) -> dict:
    match = _BRASSRING_PRELOAD_RE.search(page_html)
    if not match:
        raise ValueError("no preLoadJSON on page (not a BrassRing Talent Gateway, or blocked)")
    # Unescape from the attribute's start to the end of the page and decode
    # one object off the front; whatever page follows the object is ignored.
    data, _ = json.JSONDecoder().raw_decode(html.unescape(page_html[match.end():]))
    return data


def _brassring_answers(questions: list[dict]) -> dict[str, str]:
    """A posting's question list as {name: value}.

    The list call names fields by their internal key ("jobtitle", "formtext23")
    under "Value"; the detail page names them by their display label ("City",
    "Your skills and experience") under "AnswerValue". Unlabeled entries (the
    req id, site id, coordinates) are dropped.
    """
    answers: dict[str, str] = {}
    for question in questions:
        name = (question.get("QuestionName") or "").strip()
        value = question.get("Value") if "Value" in question else question.get("AnswerValue")
        if name and value not in (None, ""):
            answers[name] = str(value)
    return answers


def _brassring_facet_filter(facets: list[dict], allowed: list[str]) -> list[dict]:
    """The FacetFilterFields entries selecting the named facet options.

    Names are matched case-insensitively against every facet's option names
    and values (UBS's City facet shows "Zürich" but posts "Zurich"), so the
    slug can name a city without knowing which tenant-specific form field
    ("formtext2") the facet lives in.
    """
    wanted = {name.strip().lower(): name.strip() for name in allowed if name.strip()}
    matched: set[str] = set()
    selected: list[dict] = []
    for facet in facets:
        options = []
        for option in facet.get("Options") or []:
            keys = {
                (option.get("OptionName") or "").strip().lower(),
                (option.get("OptionValue") or "").strip().lower(),
            }
            hit = keys & set(wanted)
            if hit:
                matched |= hit
                options.append({**option, "Selected": True})
        if options:
            selected.append({"Name": facet.get("Name"), "Options": options})
    for key in sorted(set(wanted) - matched):
        logger.warning("BrassRing facet option %r is not on this board's facets", wanted[key])
    return selected


# Section labels are the tenant's own, in the posting's language (UBS: "City" /
# "Country / State" on English reqs, "Stadt" / "Land" on German ones). Whole
# words, so "Disclaimer / Policy statements" doesn't read as a state.
_BRASSRING_LOCATION_LABEL_RE = re.compile(
    r"\b(city|location|country|state|stadt|standort|land|ort|arbeitsort|ville|pays|lieu|città|paese|luogo)\b", re.I
)
_BRASSRING_CITY_LABEL_RE = re.compile(r"\b(city|stadt|ville|città)\b", re.I)
# Sections that are site chrome, not the posting: the same equal-opportunity,
# contact and "about us" text on every req. Dropped from the description so
# the scorer reads the role, not 3k chars of boilerplate per posting.
_BRASSRING_BOILERPLATE_LABEL_RE = re.compile(
    r"disclaimer|policy|contact|kontakt|about us|über uns|join us|das sind wir|comeback|how we hire|wie wir einstellen",
    re.I,
)


def _brassring_location(answers: dict[str, str]) -> str | None:
    """City first, then any other location/country field, distinct values joined."""
    ordered = sorted(
        (label for label in answers if _BRASSRING_LOCATION_LABEL_RE.search(label)),
        key=lambda label: 0 if _BRASSRING_CITY_LABEL_RE.search(label) else 1,
    )
    values = dict.fromkeys(strip_html(answers[label]) for label in ordered)
    return "; ".join(v for v in values if v) or None


def _fetch_brassring(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # The slug is "host:partnerid:siteid[:Option1|Option2]", read off the
    # Talent Gateway URL (jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?
    # partnerid=25008&siteid=5012). One tenant runs several sites — UBS keeps
    # professionals, students and apprentices on separate site ids, and each of
    # those has one id per UI language (the reqs are shared; German-language
    # reqs show up on the English site too). The optional 4th segment names
    # facet options to scope the board to server-side ("Zürich"), like the
    # Workday slug's location list; without it the board is fetched whole,
    # subject to the size ceiling above.
    parts = slug.split(":")
    if len(parts) not in (3, 4) or not all(parts[:3]) or not parts[1].isdigit() or not parts[2].isdigit():
        raise ValueError(f"BrassRing slug must be 'host:partnerid:siteid[:Opt1|Opt2]', got {slug!r}")
    host, partner_id, site_id = parts[:3]
    allowed = [name for name in parts[3].split("|") if name.strip()] if len(parts) == 4 else []
    page_url = f"https://{host}/TGnewUI/Search/home/HomeWithPreLoad"
    site_params = {"partnerid": partner_id, "siteid": site_id}

    resp = client.get(
        page_url, params={**site_params, "PageType": "searchResults"}, headers=_HTML_HEADERS, follow_redirects=True
    )
    resp.raise_for_status()
    preload = _brassring_preload(resp.text)
    rft = _BRASSRING_RFT_RE.search(resp.text)
    session = _BRASSRING_SESSION_RE.search(resp.text)
    results = preload.get("searchResultsResponse") or {}
    facets = (results.get("Facets") or {}).get("Facet") or []
    schema = json.loads(preload.get("SmartSearchJSONValue") or "{}")

    facet_filter = None
    if allowed:
        facet_filter = _brassring_facet_filter(facets, allowed)
        if not facet_filter:
            logger.info("Skipping %s (brassring): none of %s on the board", company_name, "|".join(allowed))
            return []
    else:
        total = results.get("JobsCount") or 0
        if total > _BRASSRING_MAX_UNSCOPED_POSTINGS:
            raise ValueError(
                f"board is unscoped ({total} postings, no facet scope in slug; "
                f"limit {_BRASSRING_MAX_UNSCOPED_POSTINGS})"
            )

    body = {
        "SiteId": int(site_id),
        "PartnerId": int(partner_id),
        "Keyword": "",
        "Location": "",
        "KeywordCustomSolrFields": schema.get("KeywordCustomSolrFields") or "",
        "LocationCustomSolrFields": schema.get("LocationCustomSolrFields") or "",
        "Latitude": 0,
        "Longitude": 0,
        "FacetFilterFields": {"Facet": facet_filter} if facet_filter else None,
        "SortType": "LastUpdated",
        "PageNumber": 1,
        "encryptedsessionvalue": session.group(1) if session else "",
    }
    headers = {**_HTML_HEADERS, "RFT": rft.group(1) if rft else ""}
    seen: set[str] = set()
    listed: list[tuple[str, dict, str]] = []  # (req id, list-call answers, link)
    total: int | None = None
    while True:
        page = client.post(f"https://{host}/TgNewUI/Search/Ajax/MatchedJobs", json=body, headers=headers)
        page.raise_for_status()
        data = page.json()
        if total is None:
            total = data.get("JobsCount") or 0
        new = 0
        for job in (data.get("Jobs") or {}).get("Job") or []:
            answers = _brassring_answers(job.get("Questions") or [])
            req_id = answers.get("reqid")
            if not req_id or req_id in seen:
                continue
            seen.add(req_id)
            new += 1
            listed.append((req_id, answers, job.get("Link") or ""))
        # Stop on the board's own count, with "nothing new" as the backstop for
        # a count that lies (a page past the end re-serves the last one).
        if new == 0 or len(seen) >= total:
            break
        body = {**body, "PageNumber": body["PageNumber"] + 1}

    postings: list[RawPosting] = []
    for req_id, answers, link in listed:
        url = link or f"{page_url}?partnerid={partner_id}&siteid={site_id}&PageType=JobDetails&jobid={req_id}"
        try:
            detail = client.get(
                page_url,
                params={**site_params, "PageType": "JobDetails", "jobid": req_id},
                headers=_HTML_HEADERS,
                follow_redirects=True,
            )
            detail.raise_for_status()
            job = _brassring_preload(detail.text).get("Jobdetails") or {}
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Skipping unavailable %s posting %s: %s", company_name, url, exc)
            continue
        if job.get("isActive") is False:
            logger.info("Skipping closed %s posting %s", company_name, url)
            continue
        sections = _brassring_answers(job.get("JobDetailQuestions") or [])
        # The list call's location fields are keyed by form-field id
        # ("formtext2"); the search page's facet list says which of those is
        # the City/Location facet, for the rare detail page with no labeled
        # location of its own.
        list_location = {
            facet["Name"]: answers[facet["Name"]]
            for facet in facets
            if facet.get("Name") in answers and _BRASSRING_LOCATION_LABEL_RE.search(facet.get("Description") or "")
        }
        postings.append(
            RawPosting(
                source="brassring",
                url=url,
                title=html.unescape(job.get("Title") or answers.get("jobtitle") or ""),
                company=company_name,
                # Every labeled section, label first ("Key responsibilities: …",
                # "Your skills and experience: …"); the metadata fields ride
                # along at the top, which is useful context for scoring.
                description="\n\n".join(
                    f"{label}: {strip_html(value)}"
                    for label, value in sections.items()
                    if not _BRASSRING_BOILERPLATE_LABEL_RE.search(label)
                ),
                location=_brassring_location(sections) or ("; ".join(list_location.values()) or None),
                raw={**answers, "sections": list(sections)},
            )
        )
    return postings


# --- Prospective Media "career center" (Sensirion) ---

# Prospective (a Swiss recruiting-media firm) hosts employers' job lists at
# ohws.prospective.ch/public/v1/careercenter/<id>/ — server-rendered HTML with
# offset/limit paging (verified: limit=100 honoured, an out-of-range offset
# serves an empty list) — and the job pages on the employer's own jobs.*
# domain, each carrying a schema.org JobPosting in JSON-LD with the full
# description. The list carries title, workload, employment type and site;
# the description needs one detail call per posting. Verified on Sensirion
# (career center 1000982, job pages on jobs.sensirion.com), 2026-09-07. The
# career center id is read off the employer's careers page: it fetches the
# center itself for its per-country job counts.
_PROSPECTIVE_HOST = "https://ohws.prospective.ch"
_PROSPECTIVE_PAGE_SIZE = 100
_PROSPECTIVE_LINK_RE = re.compile(r'class="itemlist_jobtitle">\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_PROSPECTIVE_WORKPLACE_RE = re.compile(r'id="workplace">(.*?)</div>', re.S)
_PROSPECTIVE_WORK_RE = re.compile(r'<div class="work">(.*?)<div id="workplace"', re.S)
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _prospective_items(page_html: str) -> list[dict]:
    """One dict per job on a career-center list page: url, title, workload,
    work (employment type + contract), location."""
    items: list[dict] = []
    # Split on the item wrapper rather than regex-matching whole items: the
    # blocks nest divs, so a non-greedy match can't find an item's end.
    for block in page_html.split('<div class="platform-item">')[1:]:
        link = _PROSPECTIVE_LINK_RE.search(block)
        if not link:
            continue
        title_html = link.group(2)
        workload = re.search(r"<span>(.*?)</span>", title_html, re.S)
        workplace = _PROSPECTIVE_WORKPLACE_RE.search(block)
        work = _PROSPECTIVE_WORK_RE.search(block)
        items.append(
            {
                "url": html.unescape(link.group(1)),
                "title": strip_html(re.sub(r"<span>.*?</span>", "", title_html, flags=re.S)),
                "workload": strip_html(workload.group(1)) if workload else "",
                "work": strip_html(work.group(1)) if work else "",
                "location": strip_html(workplace.group(1)) if workplace else "",
            }
        )
    return items


def _job_posting_ld(page_html: str) -> dict | None:
    """The page's schema.org JobPosting, if it carries one."""
    for block in _LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _fetch_prospective(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # The slug is the career center id, optionally with a UI language:
    # "1000982" or "1000982:en" (the list's labels; postings keep their own
    # language either way).
    parts = slug.split(":")
    if len(parts) not in (1, 2) or not parts[0].isdigit():
        raise ValueError(f"Prospective slug must be '<careercenter id>[:lang]', got {slug!r}")
    center_id, lang = parts[0], (parts[1] if len(parts) == 2 else "en")
    list_url = f"{_PROSPECTIVE_HOST}/public/v1/careercenter/{center_id}/"

    items: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while True:
        resp = client.get(
            list_url,
            params={"lang": lang, "offset": offset, "limit": _PROSPECTIVE_PAGE_SIZE},
            headers=_HTML_HEADERS,
            follow_redirects=True,
        )
        resp.raise_for_status()
        page = [item for item in _prospective_items(resp.text) if item["url"] not in seen]
        seen.update(item["url"] for item in page)
        items.extend(page)
        if len(page) < _PROSPECTIVE_PAGE_SIZE:
            break
        offset += _PROSPECTIVE_PAGE_SIZE

    postings: list[RawPosting] = []
    for item in items:
        try:
            detail = client.get(item["url"], headers=_HTML_HEADERS, follow_redirects=True)
            detail.raise_for_status()
        except httpx.HTTPError as exc:
            logger.info("Skipping unavailable %s posting %s: %s", company_name, item["url"], exc)
            continue
        posting = _job_posting_ld(detail.text) or {}
        # JSON-LD first; a page without it (or with an empty description)
        # falls back to the page text, which is at least the posting.
        description = strip_html(posting.get("description") or "")
        for extra in ("responsibilities", "qualifications"):
            if posting.get(extra) and strip_html(str(posting[extra])) not in description:
                description += "\n\n" + strip_html(str(posting[extra]))
        if not description:
            description = strip_html(detail.text)
        # The list's workload ("80-100%") and contract line lead the
        # description so the employment-% heuristic sees them.
        header = " | ".join(part for part in (item["workload"], item["work"]) if part)
        postings.append(
            RawPosting(
                source="prospective",
                url=item["url"],
                title=html.unescape(posting.get("title") or item["title"]),
                company=company_name,
                description=f"{header}\n\n{description}".strip() if header else description,
                location=item["location"] or None,
                raw={
                    "workload": item["workload"],
                    "work": item["work"],
                    "datePosted": posting.get("datePosted"),
                    "employmentType": posting.get("employmentType"),
                },
            )
        )
    return postings


# Google publishes every open role at Google, YouTube and DeepMind as one XML
# feed for job aggregators (~3500 postings, ~20 MB), descriptions included, so
# one request covers the company with no detail calls. The careers site's own
# search results and job pages are closed to crawlers by robots.txt; the feed
# is not, and it is the only thing this connector reads. The feed is global
# with no server-side filter, so it is scoped to the same target countries as
# Workday in code, after the download.
_GOOGLE_FEED_URL = "https://www.google.com/about/careers/applications/jobs/feed.xml"


def _google_location(location: ET.Element) -> str:
    parts = (location.findtext(tag) for tag in ("city", "state", "country"))
    return ", ".join(part.strip() for part in parts if part and part.strip())


def _fetch_google(company_name: str, slug: str, client: httpx.Client) -> list[RawPosting]:
    # The slug pipe-separates the feed's <employer> values to keep:
    # "Google|YouTube|DeepMind" are the three it lists, so a watchlist can take
    # DeepMind alone without the rest of Google.
    employers = {name.strip().lower() for name in slug.split("|") if name.strip()}
    if not employers:
        raise ValueError(f"Google slug must be 'Employer[|Employer...]', got {slug!r}")
    resp = client.get(_GOOGLE_FEED_URL)
    resp.raise_for_status()

    postings: list[RawPosting] = []
    try:
        # Each <job> is cleared once read, so the parsed tree never grows to
        # a second copy of the feed.
        for _, job in ET.iterparse(io.BytesIO(resp.content)):
            if job.tag != "job":
                continue
            locations = job.findall("locations/location")
            in_scope = (job.findtext("employer") or "").strip().lower() in employers and any(
                (loc.findtext("country") or "").strip().lower() in _WORKDAY_TARGET_COUNTRIES
                for loc in locations
            )
            if in_scope:
                # Every location a multi-site role is open in, as for Workday.
                where = dict.fromkeys(filter(None, map(_google_location, locations)))
                postings.append(
                    RawPosting(
                        source="google",
                        url=(job.findtext("url") or "").strip(),
                        title=(job.findtext("title") or "").strip(),
                        company=company_name,
                        description=strip_html(job.findtext("description") or ""),
                        location="; ".join(where) or None,
                        remote=parse_workplace_type(job.findtext("remote")),
                        raw={
                            "jobid": job.findtext("jobid"),
                            "published": job.findtext("published"),
                            "employer": job.findtext("employer"),
                            "jobtype": job.findtext("jobtype"),
                        },
                    )
                )
            job.clear()
    except ET.ParseError as exc:
        # A truncated download or an HTML error page served with a 200: skip
        # the company like any other bad board rather than end the source.
        raise ValueError(f"Google job feed is not readable XML: {exc}") from exc
    return postings


_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "smartrecruiters": _fetch_smartrecruiters,
    "personio": _fetch_personio,
    "recruitee": _fetch_recruitee,
    "workable": _fetch_workable,
    "teamtailor": _fetch_teamtailor,
    "workday": _fetch_workday,
    "join": _fetch_join,
    "bamboohr": _fetch_bamboohr,
    "avature": _fetch_avature,
    "successfactors": _fetch_successfactors,
    "icims": _fetch_icims,
    "brassring": _fetch_brassring,
    "prospective": _fetch_prospective,
    "google": _fetch_google,
}


class CompanyPagesSource:
    """Fetches postings for every company listed in config/companies.yaml."""

    name = "company_pages"

    def __init__(self, companies: list[dict] | None):
        """companies: list of {"name": str, "ats": <one of _FETCHERS>, "slug": str}.

        None is accepted and means no boards: it is what YAML gives for a
        `companies:` key whose every entry is commented out, which is how the
        template ships.
        """
        self.companies = [c for c in (companies or []) if isinstance(c, dict)]

    def fetch(self) -> FetchResult:
        postings: list[RawPosting] = []
        skipped: list[str] = []
        # Per-company raw posting count (0 when skipped, raised, or genuinely
        # empty). Carried in meta so it lands in runs.jsonl, giving the monthly
        # health check a per-company liveness history — a board silently going
        # dry looks the same as a healthy one without it. Raw (pre-dedup,
        # pre-location-filter) is deliberate: it measures board liveness, not
        # Swiss relevance, so a board full of foreign ads still reads as alive.
        company_counts: dict[str, int] = {}
        with httpx.Client(timeout=_TIMEOUT) as client:
            for company in self.companies:
                name = company.get("name") or "?"
                # An entry uncommented only partly (a name with no slug, or the
                # reverse) must cost that one board, not every board: a missing
                # key used to raise KeyError here and take the whole source down.
                missing = [k for k in ("name", "ats", "slug") if not company.get(k)]
                if missing:
                    logger.warning(
                        "Skipping %s: entry in config/companies.yaml has no %s",
                        name, " or ".join(missing),
                    )
                    skipped.append(name)
                    company_counts[name] = 0
                    continue
                ats = str(company.get("ats", "")).lower()
                fetcher = _FETCHERS.get(ats)
                if fetcher is None:
                    logger.warning(
                        "Skipping %s: unsupported ATS %r (supported: %s)",
                        name,
                        ats,
                        ", ".join(_FETCHERS),
                    )
                    skipped.append(name)
                    company_counts[name] = 0
                    continue
                try:
                    found = fetcher(company["name"], company["slug"], client)
                except (httpx.HTTPError, ValueError) as exc:
                    # ValueError covers a malformed connector-specific slug (e.g.
                    # a Workday "tenant:host:site") — skip that one company, don't
                    # crash the whole run.
                    logger.warning("Skipping %s (%s): %s", name, ats, exc)
                    skipped.append(name)
                    company_counts[name] = 0
                    continue
                postings.extend(found)
                company_counts[name] = len(found)
        detail = (
            f"{len(skipped)} of {len(self.companies)} companies skipped: {', '.join(skipped)}"
            if skipped
            else ""
        )
        # A flaky board or two doesn't invalidate the source, but a large share
        # failing does: the result is then materially incomplete, and reporting
        # ok would pass it off as a quiet day. This used to degrade only when
        # EVERY company failed — which is how 26 of 76 companies could 422 on
        # every run for weeks without the report ever saying so.
        ok = not (
            self.companies
            and len(skipped) / len(self.companies) >= _DEGRADED_SKIP_FRACTION
        )
        return FetchResult(
            self.name, postings, ok=ok, detail=detail, meta={"company_counts": company_counts}
        )

"""ETH Zürich job board (jobs.ethz.ch) connector.

Unlike the ATS connectors (per-company JSON APIs), this scrapes ETH's public
job board HTML — it's the canonical home of ETH AI Center roles, and robots.txt
permits it (Allow: /). Scoped to the job-type categories chosen in
config/search.yaml (``sources.eth_jobs.job_types``), filtered server-side via
the board's POST search form, which needs a Yii CSRF token taken from a prior
GET.

Listing-level only: title / location / department / workload — no per-posting
detail fetch, to keep it to a few requests per run and a single HTML structure
to maintain. If scoring needs richer text, add a detail-page fetch later.
"""

from __future__ import annotations

import logging
import re

import httpx

from ...config import DEFAULT_ETH_JOB_TYPES
from .base import FetchResult, RawPosting, strip_html

logger = logging.getLogger(__name__)

_BASE = "https://jobs.ethz.ch"
_TIMEOUT = 20.0
_HEADERS = {"User-Agent": "Mozilla/5.0 jobradar"}
# The jobtype_id values known from the board's filter form (Stellentyp) live in
# jobradar.config (ETH_JOB_TYPES), next to the setting that picks them.


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="_csrf-frontend"\s+value="([^"]+)"', html)
    return m.group(1) if m else ""


def _location_from_details(details: str) -> str | None:
    # details look like "80%-100%, Zurich, fixed-term"; the middle field is the
    # location. Fall back to the whole string so the location filter still has
    # something to match.
    parts = [p.strip() for p in details.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[1]
    return details or None


def _parse_listing(html: str) -> list[RawPosting]:
    postings: list[RawPosting] = []
    for block in re.findall(r'<li class="job-ad__item__wrapper".*?</li>', html, re.S):
        href = re.search(r'href="(/job/view/[^"]+)"', block)
        title = re.search(r'job-ad__item__title">(.*?)</h3>', block, re.S)
        if not (href and title):
            continue
        title_text = strip_html(title.group(1))
        details_m = re.search(r'job-ad__item__details">(.*?)</div>', block, re.S)
        company_m = re.search(r'job-ad__item__company">(.*?)</div>', block, re.S)
        details = strip_html(details_m.group(1)) if details_m else ""
        # company line is "<date> | <department>"; keep the department.
        company_line = strip_html(company_m.group(1)) if company_m else ""
        department = company_line.split("|", 1)[1].strip() if "|" in company_line else company_line
        description = f"ETH Zürich role: {title_text}. {details}."
        if department:
            description += f" Unit: {department}."
        postings.append(
            RawPosting(
                source="eth_jobs",
                url=_BASE + href.group(1),
                title=title_text,
                company="ETH Zürich",
                description=description,
                location=_location_from_details(details),
                raw={"details": details, "department": department},
            )
        )
    return postings


class EthJobsSource:
    """Scrapes jobs.ethz.ch, scoped to the given job-type categories."""

    name = "eth_jobs"

    def __init__(self, jobtype_ids: tuple[int, ...] = DEFAULT_ETH_JOB_TYPES):
        self.jobtype_ids = tuple(jobtype_ids)

    def fetch(self) -> FetchResult:
        postings: list[RawPosting] = []
        try:
            with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                token = _extract_csrf(client.get(_BASE + "/").text)
                for jobtype_id in self.jobtype_ids:
                    resp = client.post(
                        _BASE + "/",
                        data={
                            "_csrf-frontend": token,
                            "JobSearch[jobtype_id]": str(jobtype_id),
                            "JobSearch[region_id]": "",
                            "JobSearch[workload_id]": "",
                        },
                    )
                    resp.raise_for_status()
                    postings.extend(_parse_listing(resp.text))
        except httpx.HTTPError as exc:
            logger.warning("ETH jobs fetch failed: %s", exc)
            return FetchResult(self.name, [], ok=False, detail=str(exc))

        # A role could appear under both categories — dedupe by URL.
        seen: set[str] = set()
        deduped = [p for p in postings if not (p.url in seen or seen.add(p.url))]
        return FetchResult(
            self.name, deduped, ok=True, detail=f"{len(deduped)} postings ({len(self.jobtype_ids)} categories)"
        )

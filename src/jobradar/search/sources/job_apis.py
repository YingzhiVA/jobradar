"""Public job-board aggregator APIs (no scraping, no auth).

Arbeitnow (https://www.arbeitnow.com/api/job-board-api) is the first
connector: a free, public, unauthenticated JSON API skewed toward
European/remote tech roles, which gives reasonable DACH-adjacent coverage
without needing a paid aggregator. Its exact field names and any rate
limits should be re-verified against the live API when this is actually
wired up — treat this implementation as a starting point, not a guarantee.

Client-side keyword filtering (on title/description) is used instead of
relying on undocumented/unverified server-side filter query params, so this
connector degrades gracefully even if the API's filtering options have
changed.
"""

from __future__ import annotations

import logging

import httpx

from .base import FetchResult, RawPosting, strip_html

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_MAX_PAGES = 5


class ArbeitnowSource:
    """Fetches and keyword-filters postings from the Arbeitnow job board API."""

    name = "arbeitnow"
    _BASE_URL = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, keywords: list[str]):
        """keywords: role titles/terms to match against, e.g. ["Product Manager"].

        A posting is kept if any keyword appears (case-insensitive) in its
        title or description. Region/location filtering happens later in
        filters.py against config/constraints.yaml — this connector only
        narrows by role relevance.
        """
        self.keywords = [k.lower() for k in keywords]

    def fetch(self) -> FetchResult:
        if not self.keywords:
            return FetchResult(self.name, [], ok=True, detail="no keywords configured")
        postings: list[RawPosting] = []
        ok = True
        with httpx.Client(timeout=_TIMEOUT) as client:
            url: str | None = self._BASE_URL
            page = 0
            while url and page < _MAX_PAGES:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    payload = resp.json()
                except httpx.HTTPError as exc:
                    logger.warning("Arbeitnow fetch failed: %s", exc)
                    ok = False
                    break
                for job in payload.get("data", []):
                    title = job.get("title", "")
                    description = strip_html(job.get("description", ""))
                    haystack = f"{title} {description}".lower()
                    if any(k in haystack for k in self.keywords):
                        postings.append(
                            RawPosting(
                                source="arbeitnow",
                                url=job.get("url", ""),
                                title=title,
                                company=job.get("company_name", ""),
                                description=description,
                                location=job.get("location"),
                                raw=job,
                            )
                        )
                url = (payload.get("links") or {}).get("next")
                page += 1
        return FetchResult(self.name, postings, ok=ok, detail=f"{len(postings)} postings")

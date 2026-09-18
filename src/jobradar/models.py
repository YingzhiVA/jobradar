"""Shared data structures that flow through the pipeline after sourcing.

search/sources/base.py's RawPosting is what connectors hand back; everything from
normalize.py onward operates on the types below.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def posting_id(url: str, title: str, company: str, source: str) -> str:
    """Stable dedup key. Prefers the URL (exact match) since different ATSs
    encode uniqueness differently (e.g. some put the real job ID in a query
    string), so URLs should not be canonicalized/stripped here.
    """
    basis = url or f"{source}:{company}:{title}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass
class Posting:
    """A posting after normalize.py has filled in structured fields."""

    id: str
    source: str
    url: str
    title: str
    company: str
    description: str
    location_text: str | None = None
    # Swiss canton (e.g. "Zurich", "Vaud", "Geneva") if resolved — more
    # robust than location_text for matching, since postings usually name a
    # town rather than the canton it's in. See normalize.py's canton
    # resolution. None for non-Swiss postings or when it couldn't be resolved.
    canton: str | None = None
    remote: bool | None = None
    # The workload range the posting offers, e.g. "80-100%" -> (80, 100);
    # a single figure or "full-time" -> (x, x). None means unknown.
    employment_pct_min: int | None = None
    employment_pct_max: int | None = None
    office_days_per_week: int | None = None
    sponsorship_mentioned: bool | None = None
    # What this run established about the URL still being open — one of the
    # LIVENESS_* values in search/sources/base.py. Carried from the RawPosting
    # so the report can flag a link nothing actually confirmed; see
    # search/liveness.py. Defaults to the benign state, matching RawPosting.
    liveness: str = "listed"


@dataclass
class Constraints:
    allowed_countries: list[str]
    allowed_cities: list[str]
    allowed_cantons: list[str]
    remote_ok: bool
    min_percentage: int
    max_percentage: int
    max_office_days_per_week: int | None
    requires_sponsorship: bool
    # Countries whose *remote* postings are acceptable even though no canton
    # can be resolved for them. Distinct from remote_ok, which accepts a remote
    # role wherever on earth it's scoped; this accepts one only when the
    # posting's own location line places it in a listed country. See
    # filters.remote_country_ok. Grouped with the other location fields in
    # constraints.yaml — it only sits down here because dataclass ordering
    # puts defaulted fields last, and defaulting it keeps it optional.
    remote_countries: list[str] = field(default_factory=list)
    other_dealbreakers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Constraints":
        locations = data.get("locations", {})
        employment = data.get("employment", {})
        office_days = data.get("office_days", {})
        work_auth = data.get("work_authorization", {})
        return cls(
            allowed_countries=locations.get("allowed_countries", []),
            allowed_cities=locations.get("allowed_cities", []),
            allowed_cantons=locations.get("allowed_cantons", []),
            remote_ok=bool(locations.get("remote_ok", False)),
            remote_countries=locations.get("remote_countries") or [],
            min_percentage=int(employment.get("min_percentage", 0)),
            max_percentage=int(employment.get("max_percentage", 100)),
            max_office_days_per_week=office_days.get("max_per_week"),
            requires_sponsorship=bool(work_auth.get("requires_sponsorship", False)),
            other_dealbreakers=data.get("other_dealbreakers") or [],
        )


@dataclass
class ScoredPosting:
    posting: Posting
    skill_score: int
    interest_score: int
    best_cv: str
    brief_reason: str
    # Hard requirements the scorer judged the candidate does NOT meet, after
    # matching._filter_gaps drops the categories the prompt forbids listing.
    # These are the knockouts that capped skill_score (matching._cap_skill_score
    # applies the cap to this filtered list, not the scorer's raw one).
    # Empty when nothing was flagged.
    # Kept structured so the report's "dropped" section can show a crisp reason.
    unmet_hard_requirements: list[str] = field(default_factory=list)

    @property
    def combined_score(self) -> float:
        return (self.skill_score + self.interest_score) / 2

    @property
    def top_axis_score(self) -> int:
        """The stronger of the two axes — used to rank dropped postings so the
        ones that were close on at least one dimension (the most audit-worthy
        near-misses) surface first.
        """
        return max(self.skill_score, self.interest_score)


@dataclass
class FinalPosting:
    scored: ScoredPosting
    tier: str  # "best" | "okay"
    writeup: str

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Protocol


def strip_html(raw: str) -> str:
    """Very small HTML-to-text fallback for descriptions that come as HTML.

    Several source APIs (Greenhouse, Arbeitnow) return HTML-entity-escaped
    HTML (e.g. literal "&lt;h2&gt;"), so entities are unescaped before tags
    are stripped, or the escaped tags survive as text noise.

    Good enough for feeding a posting description to an LLM; not meant to be
    a real HTML parser.
    """
    text = html.unescape(raw)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# How much this run actually knows about whether a posting's URL is still open.
# Only LIVENESS_UNVERIFIED is user-facing (the report flags it); the other two
# both mean "no reason to doubt it", but for different reasons worth keeping
# apart when reading a run log.
#
# The posting came out of a source's own listing, fetched live this run, and was
# never probed individually — an ATS that lists a role is stating it's open.
LIVENESS_LISTED = "listed"
# The URL was fetched and answered with a reachable status.
LIVENESS_CONFIRMED = "confirmed"
# The URL was fetched and the fetch settled nothing: a 403 that survived the
# plain-UA retry, a network error, an exhausted redirect budget. liveness.py's
# recall bias keeps these (a live role shouldn't die on a blip), but "we kept it"
# is not "we checked it", and the difference has to reach the report — a 403 from
# a bot-blocking host looks identical to a live posting and can mask a real 410.
LIVENESS_UNVERIFIED = "unverified"

# Whether the employer's own job listing, fetched in this same run, backs up a
# lead that came from somewhere else (web_search). See corroborate.py.
CORROBORATION_NONE = "none"  # no listing to check against — says nothing
CORROBORATION_PRESENT = "present"  # the employer lists a matching role
CORROBORATION_ABSENT = "absent"  # the employer's listing has no such role


# Maps the workplace-type vocabularies the job-board APIs use onto a bool.
# Lever spells them lowercase ("remote"/"hybrid"/"onsite"), join.com uppercase
# ("REMOTE"/"HYBRID"/"ONSITE"), so the lookup is case-folded.
_WORKPLACE_TYPE_REMOTE = {
    "remote": True,
    "fully_remote": True,
    "onsite": False,
    "on-site": False,
    "on_site": False,
    "hybrid": False,
}


def parse_workplace_type(value: str | None) -> bool | None:
    """A board's workplace-type string as a remote flag, or None when the board
    said nothing (or said something we don't recognise) and the text heuristics
    should decide instead.
    """
    if not value:
        return None
    return _WORKPLACE_TYPE_REMOTE.get(value.strip().lower().replace(" ", "_"))


@dataclass
class RawPosting:
    """A job posting as fetched from a source, before normalization."""

    source: str
    url: str
    title: str
    company: str
    description: str
    location: str | None = None
    # Whether the source's own structured data says this role is remote. Set
    # only by connectors whose API carries an explicit field for it (Lever's
    # workplaceType, Ashby/BambooHR's isRemote, ...); None means the source
    # didn't say, and normalize.py falls back to sniffing the location text.
    # Worth threading through rather than re-deriving: a board that states
    # "remote" alongside a country-level location ("Switzerland") gives the
    # text heuristic nothing to go on.
    remote: bool | None = None
    raw: dict = field(default_factory=dict)
    # Provenance-quality fields, set by liveness.py and corroborate.py rather
    # than by the connector. They default to the benign value so a connector
    # that says nothing isn't silently treated as suspect.
    liveness: str = LIVENESS_LISTED
    corroboration: str = CORROBORATION_NONE


@dataclass
class FetchResult:
    """Outcome of one source's fetch: the postings it produced plus whether it
    completed cleanly.

    `ok=False` marks a *degraded* source — it raised, timed out, or produced
    output we couldn't use. This is what lets the daily report distinguish a
    genuine "nothing matched today" from "a source silently failed, so the
    empty result is meaningless". `detail` is a short human-readable note
    (search count, error summary, companies skipped) for the logs/report.

    `meta` carries structured, source-specific observability data (not for the
    end user) that the per-run artifact records — e.g. web_search's actual query
    strings and its discovery funnel. Empty for sources with nothing extra to say.
    """

    name: str
    postings: list[RawPosting] = field(default_factory=list)
    ok: bool = True
    detail: str = ""
    meta: dict = field(default_factory=dict)


class Source(Protocol):
    """A connector that knows how to fetch postings from one place."""

    name: str

    def fetch(self) -> FetchResult: ...

"""Suggests new config/companies.yaml entries from two sources:

  * config/seed_companies.txt — curated names pasted from structured
    directories the LLM web search won't fully recall.
  * web search — companies operating in Switzerland that fit your profile and
    are likely to be on a supported ATS. The ATS platforms have no
    cross-company "who's hiring in Switzerland" query (they're per-tenant board
    APIs), so web search fills the *discovery* half — surfacing company NAMES —
    while the ATS APIs remain the source of truth for the actual postings.

This is a setup-time/occasional helper, not part of the daily run:

    python -m jobradar.discovery.discover                 # seeds + web search
    python -m jobradar.discovery.discover --no-web-search  # seeds only (no LLM web call)

Candidate names (from either source) are probed against the supported ATSs'
public APIs (Greenhouse / Lever / Ashby / Personio / Recruitee / Workable /
Teamtailor / join / BambooHR / SmartRecruiters) with a handful of common slug
guesses (GET the board and check it has at least one live ad). Because the
probe is ground truth, the discovery step can be fuzzy: a wrong or hallucinated
name simply fails the probe and is dropped, so false positives cost nothing. A
real board with no current openings is treated the same as no board — it isn't
suggested until it's actually hiring, which also stops an empty board from
shadowing a live one on a later ATS.

Results are written to data/discovered_companies.yaml as SUGGESTIONS, never
auto-merged into companies.yaml: slug-guessing can still produce false positives
(a guessed slug coincidentally belonging to an unrelated company of a similar
name). Greenhouse, SmartRecruiters, Recruitee and Workable expose a company_name
to sanity-check against — Lever, Ashby and BambooHR don't, so review those
manually.

When a review turns up a collision — a guessed slug that resolves to a live
board of an unrelated, similarly-named company (e.g. "Hamilton" ->
Hamilton Insurance Group, "eSMART" -> "eSmart Systems") — flag it with:

    python -m jobradar.discovery.discover --reject "Company Name:ats:slug"

That records the board in the ledger's per-company rejection list, which
probe_company then skips forever. Nothing else can stop a live collision board
re-matching: it has real ads, so the ≥1-ad rule passes, and it's the same slug
on every run, so no TTL or probe-version bump helps.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic
import httpx
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from . import discovery_ledger
from ..llm import model_for, web_search_tool_type
from ..matching import load_profile
from ..search import company_health
from ..search.sources.company_pages import _FETCHERS, join_jobs_page, parse_join_company
from ..search.sources.web_search import _extract_json

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]

_TIMEOUT = 10.0

# web_search-backed company-name discovery. Haiku + the basic web_search tool is
# cheap and sufficient here (we only need names; the ATS probe verifies them).
# Resolved per call via jobradar.llm (so a value in .env is honoured); the
# web_search tool version follows the model.
_DISCOVERY_MODEL_VAR = "JOBRADAR_DISCOVERY_MODEL"
_DISCOVERY_MAX_SEARCHES = int(os.environ.get("JOBRADAR_DISCOVERY_MAX_USES", "3"))
_DISCOVERY_TIMEOUT = float(os.environ.get("JOBRADAR_DISCOVERY_TIMEOUT", "300"))
# How long a "dropped" (no ATS found) verdict stays trusted before discovery
# re-checks the company — it may have adopted a supported ATS since.
_DROP_TTL_DAYS = int(os.environ.get("JOBRADAR_DISCOVERY_DROP_TTL_DAYS", "30"))
# The same for a "matched" verdict. Longer, because a live board rarely moves —
# but not infinite: a company can leave its ATS or empty its board, and a match
# that never expired could never be re-examined.
_MATCH_TTL_DAYS = int(
    os.environ.get("JOBRADAR_DISCOVERY_MATCH_TTL_DAYS", str(discovery_ledger.DEFAULT_MATCH_TTL_DAYS))
)
# Cap how many known names we list in the exclusion prompt — input is cheap, but
# keep the prompt bounded; the most-recently-relevant ones are enough to steer.
_MAX_EXCLUSIONS_IN_PROMPT = 400
# A configured board that has produced no postings for this many days is
# flagged for review. Long enough that a startup quiet for a few weeks isn't
# flagged; short enough that a dead/migrated board surfaces within a monthly
# cycle or two. Advisory only — a false flag costs a glance, not an action.
_HEALTH_STALE_DAYS = int(os.environ.get("JOBRADAR_HEALTH_STALE_DAYS", "45"))
_LEDGER_PATH = ROOT / "data" / "discovery_ledger.json"
# Written by this module, never hand-edited (each run replaces the file
# wholesale), which is why it sits in data/ with the other generated state
# rather than in config/ among the files the user owns.
_SUGGESTIONS_PATH = ROOT / "data" / "discovered_companies.yaml"
_RUNS_PATH = ROOT / "reports" / "runs.jsonl"
_HEALTH_REPORT_PATH = ROOT / "reports" / "company_health.md"
_SEED_PATH = ROOT / "config" / "seed_companies.txt"

# Personio is an XML feed (not JSON) — handled specially in probe_company.
# join is an HTML careers page (its company data is embedded Next.js JSON,
# not a plain REST response) — also handled specially, via parse_join_company,
# plus a second call to confirm the company has any ads at all: join.com serves
# a profile page even for non-customers, so the page's existence proves nothing.
# SmartRecruiters is probed last: unlike the others, its API returns HTTP 200
# for *any* slug (with totalFound=0 for non-existent companies), so it must be
# validated on totalFound, not status — see probe_company.
_ATS_PROBE_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "personio": "https://{slug}.jobs.personio.de/xml",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",
    "teamtailor": "https://{slug}.teamtailor.com/jobs.json",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    "join": "https://join.com/companies/{slug}",
    # BambooHR hosts each customer at {slug}.bamboohr.com; a non-customer subdomain
    # 302-redirects to the marketing site (status != 200, filtered out below), so a
    # 200 whose body carries a `result` list is a genuine board.
    "bamboohr": "https://{slug}.bamboohr.com/careers/list",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
}

# Identifies the current set of supported ATSs. Stamped onto "dropped" ledger
# entries so that when this set grows (a new connector is added), prior drops are
# re-probed against the larger set instead of staying suppressed by the TTL.
_PROBE_VERSION = ",".join(sorted(_ATS_PROBE_URLS))


def _personio_has_ads(text: str) -> bool:
    """Personio's feed is XML; a real board's root is <workzag-jobs> and each ad
    is a <position>. Require ≥1 position — an empty board proves nothing and
    could shadow a live board on a later ATS. A non-existent slug 307-redirects
    (filtered on status before we get here).
    """
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        # ValueError: some Python builds reject a str carrying an XML encoding
        # declaration — treat as "not a board" rather than crash the probe.
        return False
    return root.tag == "workzag-jobs" and root.find(".//position") is not None

_LEGAL_SUFFIXES_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|llc|ltd|limited|gmbh|ag|sa|plc|co)\.?\b", re.I
)


def _slug_candidates(company_name: str) -> list[str]:
    """A handful of common ATS slug conventions, most-likely first."""
    stripped = _LEGAL_SUFFIXES_RE.sub("", company_name).strip()
    candidates = []
    for name in dict.fromkeys([stripped, company_name]):  # dedupe, keep order
        if not name:
            continue
        compact = re.sub(r"[^a-z0-9]", "", name.lower())
        hyphenated = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if compact:
            candidates.append(compact)
        if hyphenated and hyphenated != compact:
            candidates.append(hyphenated)
    return list(dict.fromkeys(candidates))


class CompanyMatch(BaseModel):
    name: str
    ats: str
    slug: str
    verified_company_name: str | None = None  # only Greenhouse's API exposes this


def _skip_set(
    skip: tuple[str, str] | Iterable[tuple[str, str]] | None,
) -> frozenset[tuple[str, str]]:
    """Normalize probe_company's `skip` to a set of (ats, slug) pairs. Accepts a
    single bare pair (the health-check migration case) or an iterable of pairs
    (a company's ledger rejections), so callers can pass either."""
    if not skip:
        return frozenset()
    if isinstance(skip, tuple) and len(skip) == 2 and all(isinstance(x, str) for x in skip):
        return frozenset({skip})  # a single (ats, slug) pair
    return frozenset(tuple(pair) for pair in skip)


def probe_company(
    company_name: str,
    client: httpx.Client,
    skip: tuple[str, str] | Iterable[tuple[str, str]] | None = None,
) -> CompanyMatch | None:
    """Tries slug guesses against each ATS in turn; returns the first board that
    has at least one live ad.

    A match requires ≥1 posting on EVERY ATS, not just the ones whose API 200s
    for any slug. An empty board proves nothing we can act on, and — because
    ATSs are tried in a fixed order and the first hit wins — an empty board
    earlier in the sequence would otherwise shadow a company's real, live board
    later (the generalized form of the join-shadows-smartrecruiters bug). The
    cost is one board that exists but has no current openings won't be
    discovered until it posts something; that's acceptable for a discovery
    helper (it resurfaces once it's hiring).

    `skip` is an (ats, slug) pair — or an iterable of them — to pass over. It
    covers two cases: the configured board when re-probing to find where a
    company moved, and boards a human has flagged as wrong-company slug
    collisions (a live collision board has real ads, so nothing else here can
    stop it re-matching — see discovery_ledger.reject_board).
    """
    skip_set = _skip_set(skip)
    for slug in _slug_candidates(company_name):
        for ats, url_template in _ATS_PROBE_URLS.items():
            if (ats, slug) in skip_set:
                continue
            url = url_template.format(slug=slug)
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue

            # Personio is XML, not JSON — validate and short-circuit here.
            if ats == "personio":
                if _personio_has_ads(resp.text):
                    return CompanyMatch(name=company_name, ats=ats, slug=slug)
                continue

            # join is an HTML page with the company data embedded as Next.js
            # JSON — validate and short-circuit here too.
            if ats == "join":
                company = parse_join_company(resp.text)
                if company is None:
                    continue
                # A company PAGE is not a job board: join.com serves one for
                # companies that aren't customers (Swisscom has a join profile
                # with no ads while its real board is Workday). So require at
                # least one actual ad, same as every other ATS here.
                try:
                    if not join_jobs_page(client, company["id"]).get("items"):
                        continue
                except (httpx.HTTPError, KeyError, ValueError):
                    continue
                return CompanyMatch(
                    name=company_name, ats=ats, slug=slug, verified_company_name=company.get("name")
                )

            try:
                data = resp.json()
            except ValueError:
                continue

            # Require ≥1 posting for every ATS. For most, the probe response
            # already carries the full list, so this is a presence-of-content
            # check with no extra HTTP.
            if ats == "lever" and not (isinstance(data, list) and data):
                continue
            if ats in ("greenhouse", "ashby") and not data.get("jobs"):
                continue
            if ats == "recruitee" and not data.get("offers"):
                continue
            if ats == "teamtailor" and not data.get("items"):
                continue
            if ats == "bamboohr" and not data.get("result"):
                continue
            if ats == "smartrecruiters" and not data.get("totalFound"):
                continue
            if ats == "workable" and not data.get("jobs"):
                continue

            verified_name = None
            if ats == "greenhouse":
                verified_name = data["jobs"][0].get("company_name")
            elif ats == "smartrecruiters" and data.get("content"):
                verified_name = (data["content"][0].get("company") or {}).get("name")
            elif ats == "recruitee":
                verified_name = data["offers"][0].get("company_name")
            elif ats == "workable":
                verified_name = data.get("name")
            elif ats == "teamtailor":
                jp = data["items"][0].get("_jobposting") or {}
                verified_name = (jp.get("hiringOrganization") or {}).get("name")

            return CompanyMatch(name=company_name, ats=ats, slug=slug, verified_company_name=verified_name)
    return None


class _DiscoveredNames(BaseModel):
    companies: list[str]


_COMPANY_SEARCH_PROMPT = """\
Find real companies that operate in Switzerland (headquartered here, or with a \
Swiss office) worth scanning for roles that would fit this candidate:

{intent}

Focus on tech- and product-driven employers — startups, scale-ups, and modern \
enterprises — since those are the ones most likely to post on an applicant- \
tracking platform such as Greenhouse, Lever, Ashby, SmartRecruiters or Personio.

{exclusion}Return ONLY company NAMES (not job postings, not links). Cast a wide \
net — it's fine to include companies you're unsure about, because each name is \
verified against the real ATS APIs afterwards, so a wrong guess costs nothing.

Respond with ONLY a JSON object (no prose, no markdown fences):
{{"companies": ["Company A", "Company B"]}}
"""


def _exclusion_clause(exclude: list[str] | None) -> str:
    if not exclude:
        return ""
    names = ", ".join(exclude[:_MAX_EXCLUSIONS_IN_PROMPT])
    return (
        "We have ALREADY considered the companies below — do NOT list any of "
        "them; spend your answer on companies NOT in this list:\n"
        f"{names}\n\n"
    )


def search_company_names(
    client: anthropic.Anthropic,
    intent: str,
    max_searches: int = _DISCOVERY_MAX_SEARCHES,
    exclude: list[str] | None = None,
) -> list[str]:
    """Use web search to surface Swiss-operating company NAMES that fit the
    candidate. `exclude` lists companies already known/checked so the model
    spends its budget on new ones. Degrades to [] on any failure — discovery
    should never crash the run, and the seed-list path still works without it.
    """
    try:
        model = model_for(_DISCOVERY_MODEL_VAR)
        response = client.with_options(
            timeout=_DISCOVERY_TIMEOUT, max_retries=0
        ).messages.create(
            model=model,
            # Headroom for a long list of names; 1024 risked truncating the JSON
            # into an unparseable (silently-empty) result.
            max_tokens=2048,
            tools=[{"type": web_search_tool_type(model), "name": "web_search", "max_uses": max_searches}],
            messages=[
                {
                    "role": "user",
                    "content": _COMPANY_SEARCH_PROMPT.format(
                        intent=intent or "(no specific profile provided)",
                        exclusion=_exclusion_clause(exclude),
                    ),
                }
            ],
        )
        text = "\n".join(b.text for b in response.content if b.type == "text")
        names = _DiscoveredNames.model_validate(_extract_json(text)).companies
        return [n.strip() for n in names if n and n.strip()]
    except (ValueError, ValidationError) as exc:
        logger.warning("Company-name web search returned unparseable output: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001 - discovery degrades, never crashes the run
        logger.warning("Company-name web search failed: %s", exc)
        return []


def load_seed_companies(path: Path) -> list[str]:
    """Read curated candidate company names from a seed file (one per line;
    blank lines and #-comments ignored). These are pasted from structured
    directories the LLM web search won't fully recall — the long-tail half of
    discovery. Missing file is fine (returns []).
    """
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def _discovery_intent(identity: str) -> str:
    """A short profile description to steer the web company search — the
    candidate's identity statement.
    """
    return identity.strip()


def discover(
    existing_companies: list[dict],
    client: anthropic.Anthropic,
    *,
    use_web_search: bool = True,
    web_intent: str = "",
    seed_names: list[str] | None = None,
    ledger: discovery_ledger.Ledger | None = None,
    now: datetime | None = None,
    ttl_days: int = _DROP_TTL_DAYS,
    match_ttl_days: int = _MATCH_TTL_DAYS,
) -> list[CompanyMatch]:
    now = now or datetime.now(timezone.utc)
    already_configured = {c["name"].strip().lower() for c in existing_companies if c.get("name")}

    names: list[str] = list(seed_names or [])
    if use_web_search:
        # Exclude what we've already settled (configured + still-authoritative
        # ledger entries) so the search budget goes to genuinely new names.
        # Stale verdicts are intentionally NOT excluded — they're due for re-check.
        exclude = sorted({c["name"] for c in existing_companies if c.get("name")})
        if ledger:
            exclude += discovery_ledger.active_names(
                ledger, now, ttl_days, _PROBE_VERSION, match_ttl_days
            )
        web_names = search_company_names(client, web_intent, exclude=exclude)
        logger.info(
            "web search suggested %d candidate compan%s",
            len(web_names),
            "y" if len(web_names) == 1 else "ies",
        )
        names += web_names

    # Dedupe case-insensitively (across both sources), dropping already-configured
    # and anything the ledger already settled (a still-fresh match or drop).
    seen: set[str] = set()
    candidate_names: list[str] = []
    for name in names:
        key = name.strip().lower()
        if not key or key in already_configured or key in seen:
            continue
        if ledger is not None and discovery_ledger.should_skip_probe(
            ledger, name, now, ttl_days, _PROBE_VERSION, match_ttl_days
        ):
            continue
        seen.add(key)
        candidate_names.append(name)

    matches = []
    with httpx.Client(timeout=_TIMEOUT) as http_client:
        for name in candidate_names:
            # Skip any board a human has flagged as a wrong-company collision for
            # this name, so it can never be re-matched (see reject_board).
            skip = discovery_ledger.rejected_boards(ledger, name) if ledger is not None else set()
            match = probe_company(name, http_client, skip=skip)
            if match:
                matches.append(match)
                if ledger is not None:
                    discovery_ledger.record(ledger, name, "matched", now, _PROBE_VERSION)
            else:
                # Derived from the probe table rather than hand-listed: the
                # hardcoded version had already drifted (it omitted join).
                logger.info(
                    "No ATS board found for %r (tried %s slug guesses)",
                    name,
                    "/".join(sorted(_ATS_PROBE_URLS)),
                )
                if ledger is not None:
                    discovery_ledger.record(ledger, name, "dropped", now, _PROBE_VERSION)
    return matches


def render_suggestions_yaml(matches: list[CompanyMatch], run_date: date) -> str:
    lines = [
        f"# Auto-discovered on {run_date.isoformat()}.",
        "# These are SUGGESTIONS based on guessed ATS slugs, not confirmed matches —",
        "# verify each one (e.g. open the URL) before copying it into companies.yaml.",
        "# Greenhouse and SmartRecruiters hits include the API's own company_name",
        "# for a quick sanity check; Lever and Ashby don't expose one.",
        "",
    ]
    if not matches:
        lines.append("# No new companies found this run.")
        return "\n".join(lines) + "\n"

    lines.append("companies:")
    for m in matches:
        lines.append(f"  - name: {m.name}")
        lines.append(f"    ats: {m.ats}")
        lines.append(f"    slug: {m.slug}")
        if m.verified_company_name:
            lines.append(f"    # API reports company_name: {m.verified_company_name!r}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class HealthFinding(BaseModel):
    name: str
    ats: str
    slug: str
    dry_days: int
    # "gone": configured board unreachable/404; "empty": reachable but no ads;
    # "has_ads": board has ads now though daily runs recorded none (a bug/flaky
    # signal); "unsupported": ats no longer has a connector.
    status: str
    note: str = ""
    moved_to: CompanyMatch | None = None  # a live board found on another ATS


def diagnose_stale(
    stale: list[company_health.StaleFinding],
    companies: list[dict],
    client: httpx.Client,
    ledger: discovery_ledger.Ledger | None = None,
) -> list[HealthFinding]:
    """Re-check each sustained-dry board live to explain why it's dry: gone,
    empty, or actually-has-ads (a daily-pipeline bug). For a gone/empty board,
    look for a live board on another ATS (skipping the configured one) to catch
    a migration.

    The migration search also skips any board the ledger has flagged as a
    wrong-company collision for that name, so a known-bad slug isn't suggested."""
    by_name = {c["name"]: c for c in companies if c.get("name")}
    findings: list[HealthFinding] = []
    for s in stale:
        entry = by_name.get(s.name)
        if entry is None:
            continue  # removed from config since the run history was written
        ats = (entry.get("ats") or "").lower()
        slug = entry.get("slug") or ""
        # The configured board plus any human-flagged collisions for this name.
        migration_skip = {(ats, slug)}
        if ledger is not None:
            migration_skip |= discovery_ledger.rejected_boards(ledger, s.name)
        fetcher = _FETCHERS.get(ats)
        if fetcher is None:
            findings.append(
                HealthFinding(
                    name=s.name, ats=ats, slug=slug, dry_days=s.dry_days,
                    status="unsupported", note=f"no connector for ATS {ats!r}",
                )
            )
            continue

        try:
            live_count = len(fetcher(s.name, slug, client))
        except (httpx.HTTPError, ValueError) as exc:
            findings.append(
                HealthFinding(
                    name=s.name, ats=ats, slug=slug, dry_days=s.dry_days,
                    status="gone", note=f"board unreachable: {exc}",
                    moved_to=probe_company(s.name, client, skip=migration_skip),
                )
            )
            continue

        if live_count > 0:
            findings.append(
                HealthFinding(
                    name=s.name, ats=ats, slug=slug, dry_days=s.dry_days, status="has_ads",
                    note=f"{live_count} ad(s) live now, but daily runs recorded none — "
                    "connector/pipeline issue or flakiness",
                )
            )
        else:
            findings.append(
                HealthFinding(
                    name=s.name, ats=ats, slug=slug, dry_days=s.dry_days, status="empty",
                    moved_to=probe_company(s.name, client, skip=migration_skip),
                )
            )
    return findings


_HEALTH_GROUPS = [
    ("gone", "Board gone — act (account removed / left this ATS)"),
    ("empty", "Empty — verify (not hiring, or moved to an unsupported ATS)"),
    ("has_ads", "Board has ads but daily runs saw none — investigate the connector"),
    ("unsupported", "ATS no longer supported"),
]


def render_health_report(findings: list[HealthFinding], run_date: date) -> str:
    lines = [f"# Company board health — {run_date.isoformat()}", ""]
    if not findings:
        lines.append(
            f"No configured board has been dry for {_HEALTH_STALE_DAYS}+ days. "
            "(History accrues from runs.jsonl, so this is empty until enough runs exist.)"
        )
        return "\n".join(lines) + "\n"
    lines.append(
        f"{len(findings)} configured board(s) produced nothing for {_HEALTH_STALE_DAYS}+ days, "
        "re-checked live:"
    )
    for status, heading in _HEALTH_GROUPS:
        group = [f for f in findings if f.status == status]
        if not group:
            continue
        lines += ["", f"## {heading}", ""]
        for f in group:
            moved = (
                f" **Possibly moved to {f.moved_to.ats}/{f.moved_to.slug}.**"
                if f.moved_to
                else ""
            )
            note = f" — {f.note}" if f.note else ""
            lines.append(
                f"- **{f.name}** ({f.ats}/{f.slug}) — dry {f.dry_days} days{note}.{moved}"
            )
    return "\n".join(lines) + "\n"


def run_health_check(
    existing_companies: list[dict],
    *,
    runs_path: Path = _RUNS_PATH,
    report_path: Path = _HEALTH_REPORT_PATH,
    today: date | None = None,
    stale_days: int = _HEALTH_STALE_DAYS,
    ledger: discovery_ledger.Ledger | None = None,
) -> list[HealthFinding]:
    """Flag configured boards dry for stale_days+, diagnose each live, and write
    reports/company_health.md. Best-effort — logs and returns [] on any failure
    rather than breaking the discovery run."""
    today = today or date.today()
    try:
        runs = company_health.load_runs(runs_path)
        names = [c["name"] for c in existing_companies if c.get("name")]
        stale = company_health.stale_companies(runs, names, today, stale_days)
        if not stale:
            report_path.write_text(render_health_report([], today), encoding="utf-8")
            logger.info("Board health: no board dry for %d+ days", stale_days)
            return []
        with httpx.Client(timeout=_TIMEOUT) as client:
            findings = diagnose_stale(stale, existing_companies, client, ledger=ledger)
        report_path.write_text(render_health_report(findings, today), encoding="utf-8")
        logger.warning(
            "Board health: %d board(s) dry for %d+ days — see %s",
            len(findings), stale_days, report_path,
        )
        for f in findings:
            moved = f" (possibly -> {f.moved_to.ats}/{f.moved_to.slug})" if f.moved_to else ""
            logger.warning("  %s (%s/%s): %s, dry %dd%s", f.name, f.ats, f.slug, f.status, f.dry_days, moved)
        return findings
    except Exception as exc:  # noqa: BLE001 - health must never break discovery
        logger.warning("Board health check failed: %s", exc)
        return []


def _reject_collisions(specs: list[str], now: datetime | None = None) -> None:
    """Register wrong-company slug collisions and re-probe. Each spec is
    "Company Name:ats:slug" (the ats/slug being the board that is NOT this
    company). The rejection is saved to the ledger so probe_company skips that
    board forever, then the company is re-probed and its verdict re-recorded
    from whatever remains (usually 'dropped')."""
    now = now or datetime.now(timezone.utc)
    ledger = discovery_ledger.load_ledger(_LEDGER_PATH)
    with httpx.Client(timeout=_TIMEOUT) as http_client:
        for spec in specs:
            try:
                name, ats, slug = spec.rsplit(":", 2)
            except ValueError:
                logger.error("Bad --reject %r; expected 'Company Name:ats:slug'", spec)
                continue
            discovery_ledger.reject_board(ledger, name, ats.lower(), slug)
            skip = discovery_ledger.rejected_boards(ledger, name)
            match = probe_company(name, http_client, skip=skip)
            outcome = "matched" if match else "dropped"
            discovery_ledger.record(ledger, name, outcome, now, _PROBE_VERSION)
            moved = f" -> now {match.ats}/{match.slug}" if match else ""
            logger.info("Rejected %s board %s/%s; re-probe: %s%s", name, ats.lower(), slug, outcome, moved)
    discovery_ledger.save_ledger(_LEDGER_PATH, ledger)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help="Discover only from config/seed_companies.txt (skip the web_search company search).",
    )
    parser.add_argument(
        "--reject",
        action="append",
        metavar="NAME:ats:slug",
        help="Flag a slug-collision board as NOT this company (repeatable), so it's "
        "never re-matched. Runs as a standalone maintenance action and exits.",
    )
    args = parser.parse_args(argv)
    use_web_search = not args.no_web_search

    load_dotenv(ROOT / ".env")

    if args.reject:
        _reject_collisions(args.reject)
        return

    client = anthropic.Anthropic()

    companies_config = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text()) or {}
    existing_companies = companies_config.get("companies", [])

    _cvs, identity, _stories = load_profile(ROOT / "profile")
    seed_names = load_seed_companies(_SEED_PATH)
    if seed_names:
        logger.info("%d seed compan%s from %s", len(seed_names), "y" if len(seed_names) == 1 else "ies", _SEED_PATH.name)
    if not use_web_search and not seed_names:
        logger.info("No seed list and no web search — nothing to discover from.")
        return

    ledger = discovery_ledger.load_ledger(_LEDGER_PATH)
    matches = discover(
        existing_companies,
        client,
        use_web_search=use_web_search,
        web_intent=_discovery_intent(identity),
        seed_names=seed_names,
        ledger=ledger,
    )
    discovery_ledger.save_ledger(_LEDGER_PATH, ledger)

    output_path = _SUGGESTIONS_PATH
    output_path.write_text(render_suggestions_yaml(matches, date.today()), encoding="utf-8")
    logger.info(
        "Found %d candidate compan%s. Suggestions written to %s",
        len(matches),
        "y" if len(matches) == 1 else "ies",
        output_path,
    )

    # Health check on the *existing* list: flag boards that have silently gone
    # dry (a loud failure is caught by the daily degraded threshold; this is the
    # silent 200-empty case). Independent of whether new companies were found.
    # Pass the ledger so a rejected collision isn't suggested as a migration.
    run_health_check(existing_companies, ledger=ledger)


if __name__ == "__main__":
    main()

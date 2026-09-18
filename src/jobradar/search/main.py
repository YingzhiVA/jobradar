"""Orchestrates one full daily run: fetch -> dedup -> normalize -> hard
filters -> score -> rank -> write-up -> report.

Usage:
    python -m jobradar.search.main                  # real run against configured sources
    python -m jobradar.search.main --dry-run         # seeded fixture postings instead of live sources,
                                               # but still real LLM calls — use this first to
                                               # validate the pipeline before touching live APIs.
    python -m jobradar.search.main --no-web-search   # skip the web_search fallback/discovery source
    python -m jobradar.search.main --respect-schedule # no-op unless config/search.yaml says today is a run day

How much this surfaces, how good a match has to be and how often it runs are
user settings in config/search.yaml, read through jobradar.config — not
constants in this package.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

from ..config import ConfigError, Sources, load_search_settings
from .corroborate import corroborate
from .dedup import SeenStore
from .filters import company_is_named, could_pass_location, filter_postings
from ..matching import build_profile_block, load_profile, score_postings
from ..models import Constraints
from .normalize import fill_gaps, heuristic_normalize
from .observability import append_run_record, build_run_record
from .ranking import is_eligible, select_with_settings
from ..schedule import decide as decide_run_day, last_run_date
from .report import write_report
from .sources.base import (
    CORROBORATION_ABSENT,
    LIVENESS_UNVERIFIED,
    FetchResult,
    RawPosting,
)
from .sources.company_pages import CompanyPagesSource
from .sources.eth import EthJobsSource
from .sources.web_search import WebSearchSource
from .writeup import write_rationales

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]  # project root, three levels above src/jobradar/search/


class AuthenticationConfigError(RuntimeError):
    """Raised when the Anthropic client can't authenticate at all."""


def _check_authentication(client: anthropic.Anthropic) -> None:
    """Fail fast and loud if the client can't authenticate, rather than
    letting every downstream LLM call (normalize/score/write-up) silently
    fail and log a per-posting warning — which would degrade into a
    misleadingly normal-looking "no matches today" report instead of
    surfacing the real problem. models.list() is a free metadata call, so
    this costs nothing.
    """
    try:
        client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AuthenticationConfigError(
            "Anthropic API authentication failed. Locally, check ANTHROPIC_API_KEY "
            "in .env (or your `ant auth login` profile). In CI: with the "
            "ANTHROPIC_API_KEY repo secret, check its value in the Claude Console; "
            "with workload identity federation, the identity token was rejected — "
            "check the federation rule's subject_prefix against the run's branch at "
            "https://platform.claude.com/settings/workload-identity-federation?tab=history"
        ) from exc
    except TypeError as exc:
        # The SDK raises a client-side TypeError (not an API error) when no
        # credential source resolves at all — e.g. nothing in .env and no
        # `ant auth login` profile active.
        raise AuthenticationConfigError(
            "No Anthropic credentials configured. Set ANTHROPIC_API_KEY in .env, "
            "or run `ant auth login`, then re-run. In CI this means neither the "
            "ANTHROPIC_API_KEY repo secret nor the federation variables "
            "(ANTHROPIC_FEDERATION_RULE_ID / ANTHROPIC_ORGANIZATION_ID / "
            "ANTHROPIC_SERVICE_ACCOUNT_ID) resolved — set one of the two, see "
            "docs/CLOUD.md."
        ) from exc


def _build_search_intent(identity: str) -> str:
    """A natural-language description of what the candidate wants, fed to the
    profile-driven web_search source. Built from the identity statement —
    deliberately NOT a keyword list, so discovery isn't pre-gated by job-title
    strings.
    """
    return identity.strip() or "(no profile information provided)"


def _build_location_desc(constraints: Constraints) -> str:
    """A human-readable statement of the location requirements for the
    web_search prompt. Mirrors filters._location_ok()'s OR semantics.
    """
    clauses: list[str] = []
    if constraints.allowed_cantons:
        clauses.append(
            "in Switzerland, specifically the cantons of "
            + ", ".join(constraints.allowed_cantons)
            + " (including towns within those cantons)"
        )
    if constraints.allowed_cities:
        clauses.append("in " + ", ".join(constraints.allowed_cities))
    if constraints.allowed_countries:
        clauses.append("in " + ", ".join(constraints.allowed_countries))
    location = "; or ".join(clauses) if clauses else "any location"
    if constraints.remote_ok:
        location += "; fully-remote roles are also acceptable"
    elif constraints.remote_countries:
        # Not covered by the canton clause above: these roles have no town at
        # all, so a search steered only at cantons won't surface them.
        location += (
            "; fully-remote roles are also acceptable when the role is based in "
            + ", ".join(constraints.remote_countries)
        )
    return location


def _build_sources(
    companies_config: dict,
    constraints: Constraints,
    identity: str,
    client: anthropic.Anthropic,
    use_web_search: bool,
    sources_settings: Sources | None = None,
) -> list:
    # Note: the Arbeitnow connector (sources/job_apis.py) is intentionally not
    # wired in — its feed is effectively German-only, so it contributes ~zero
    # postings to a Switzerland-focused search while still costing a normalize
    # pass each. Re-add it here if/when the search broadens to EU-remote roles.
    optional = sources_settings or Sources()
    sources: list = [CompanyPagesSource(companies_config.get("companies", []))]
    # ETH's own job board, per job-type category — which categories are worth
    # scanning depends on the user's role, so it is a config switch.
    if optional.eth_jobs.enabled:
        sources.append(EthJobsSource(optional.eth_jobs.job_types))
    if use_web_search:
        sources.append(
            WebSearchSource(
                client,
                _build_search_intent(identity),
                _build_location_desc(constraints),
            )
        )
    return sources


def _fetch_all(sources: list) -> list[FetchResult]:
    """Fetch every source, returning one FetchResult each (postings + health).

    A source that raises is recorded as a degraded FetchResult rather than
    killing the run — so the run still produces a report, but one that knows it
    is incomplete (see run()'s `degraded` handling).
    """
    results: list[FetchResult] = []
    for source in sources:
        try:
            result = source.fetch()
        except Exception as exc:  # noqa: BLE001 - one bad source shouldn't kill the run
            logger.warning("%s: fetch failed: %s", source.name, exc)
            results.append(FetchResult(source.name, [], ok=False, detail=str(exc)))
            continue
        logger.info(
            "%s: fetched %d postings [%s]%s",
            result.name,
            len(result.postings),
            "ok" if result.ok else "DEGRADED",
            f" — {result.detail}" if result.detail else "",
        )
        results.append(result)
    return results


def _drop_uncorroborated(postings: list[RawPosting]) -> tuple[list[RawPosting], list[RawPosting]]:
    """Split postings into (kept, dropped), dropping only those where BOTH weak
    staleness signals agree: the liveness check couldn't confirm the link, and
    the employer's own listing this run has no such role.

    Neither signal is worth acting on alone — a 403 is routine bot protection, and
    an ATS listing can be partial or paginated short — so each on its own only
    annotates the posting (the report flags an unverified link; the run log records
    an absent one). Together they are the 2026-08-17 Swiss Re failure exactly: a
    host refusing to answer about a role its own careers site doesn't have. Drop
    those before they cost a normalize/score/write-up pass, let alone the reader's
    time.
    """
    kept: list[RawPosting] = []
    dropped: list[RawPosting] = []
    for p in postings:
        if p.liveness == LIVENESS_UNVERIFIED and p.corroboration == CORROBORATION_ABSENT:
            dropped.append(p)
            logger.info(
                "Dropping uncorroborated posting: %s — %s (%s) — link unverified "
                "and absent from the employer's own listing",
                p.company,
                p.title,
                p.url,
            )
        else:
            kept.append(p)
    return kept, dropped


def _fixture_postings() -> list[RawPosting]:
    """A small, fixed set of fake postings for --dry-run: one that should
    clear the hard filters and reach scoring, one that should be dropped on
    location, and one remote one to exercise that path too.
    """
    return [
        RawPosting(
            source="fixture",
            url="https://example.com/jobs/1",
            title="Senior Product Manager",
            company="Fixture Co",
            description=(
                "We're looking for a Senior Product Manager to own our core platform "
                "roadmap. 100% full-time. Hybrid: 2 days a week in our Zurich office. "
                "5+ years of PM experience required."
            ),
            location="Zurich, Switzerland",
        ),
        RawPosting(
            source="fixture",
            url="https://example.com/jobs/2",
            title="Warehouse Associate",
            company="Fixture Logistics",
            description="Full-time warehouse role, 5 days on-site. No remote option.",
            location="Lagos, Nigeria",
        ),
        RawPosting(
            source="fixture",
            url="https://example.com/jobs/3",
            title="Product Manager, Growth",
            company="Fixture Remote Inc",
            description=(
                "Fully remote product manager role focused on growth experimentation. "
                "80-100%. No office requirement."
            ),
            location="Remote",
        ),
    ]


def run(
    *, dry_run: bool = False, use_web_search: bool = True, respect_schedule: bool = False
) -> None:
    load_dotenv(ROOT / ".env")

    # The user-steerable dials (how many matches, what score floors, how often)
    # — read before anything is spent, so a typo'd value fails on the spot
    # rather than after a full fetch+score pass. See jobradar.config.
    settings = load_search_settings(ROOT)
    thresholds, output = settings.thresholds, settings.output
    logger.info(
        "Settings: skill floor %d, interest floor %d, best bar %d, cap %d best + %d okay, "
        "eth_jobs %s",
        thresholds.min_skill,
        thresholds.min_interest,
        thresholds.best_threshold,
        output.max_best,
        output.max_okay,
        "on" if settings.sources.eth_jobs.enabled else "off",
    )

    # Only when asked (a machine-local cron, say): an invocation someone typed
    # is a deliberate act and always runs, the same way a manual workflow
    # dispatch does. The cloud schedule gates on jobradar.schedule in the
    # workflow instead, so that a skipped day also skips the email and commit
    # steps rather than just this one.
    if respect_schedule:
        # Uses the settings already loaded above rather than re-reading them, so
        # the cadence decision and the run can't be made against two different
        # versions of the file.
        decision = decide_run_day(
            date.today(), last_run_date(ROOT / "reports"), settings.schedule
        )
        if not decision.run:
            logger.info("Not a scheduled run day — %s. Exiting.", decision.reason)
            return
        logger.info("Scheduled run day — %s.", decision.reason)

    client = anthropic.Anthropic()
    _check_authentication(client)

    companies_config = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text()) or {}
    constraints_config = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text()) or {}
    constraints = Constraints.from_dict(constraints_config)

    cvs, identity, stories = load_profile(ROOT / "profile")
    profile_block = build_profile_block(cvs, identity, stories)

    if dry_run:
        fetch_results = [FetchResult("fixture", _fixture_postings())]
    else:
        sources = _build_sources(
            companies_config, constraints, identity, client, use_web_search,
            sources_settings=settings.sources,
        )
        fetch_results = _fetch_all(sources)

    # Check leads from the open web against the employer listings this same run
    # fetched, before anything is spent on them. This stamps `corroboration`; the
    # drop below is what acts on it.
    corroboration_counts = corroborate(fetch_results)

    raw_postings = [p for r in fetch_results for p in r.postings]
    raw_postings, uncorroborated = _drop_uncorroborated(raw_postings)
    if uncorroborated:
        logger.info(
            "%d posting(s) dropped as uncorroborated (unverified link + absent from "
            "the employer's own listing)",
            len(uncorroborated),
        )
    # A run is "degraded" if any source failed to do its job (raised, timed out,
    # or produced unusable output). This is what lets the report tell a genuine
    # "nothing matched today" apart from "discovery silently broke".
    failed_sources = [r.name for r in fetch_results if not r.ok]
    degraded = bool(failed_sources)
    if degraded:
        logger.warning("Run is degraded — failed/unreliable sources: %s", ", ".join(failed_sources))
    logger.info(
        "Fetched %d raw postings total, %d carried forward",
        len(raw_postings) + len(uncorroborated),
        len(raw_postings),
    )

    # Heuristic-normalize everything first (cheap, no LLM) so we can dedup
    # before paying for the LLM gap-fill — otherwise every already-seen
    # posting fires a normalize LLM call only to be dropped on the next line.
    postings = [heuristic_normalize(raw) for raw in raw_postings]

    with SeenStore(ROOT / "data" / "seen_postings.json") as seen_store:
        unseen = seen_store.filter_unseen(postings)
        logger.info("%d/%d postings are new", len(unseen), len(postings))

        # Drop postings with no identifiable employer (e.g. web_search's
        # "Undisclosed"/"Stealth") — not actionable, and not worth an LLM pass.
        named = [p for p in unseen if company_is_named(p)]
        if len(named) < len(unseen):
            logger.info("%d/%d new postings dropped (undisclosed company)", len(unseen) - len(named), len(unseen))

        # Cheap location pre-filter: skip the LLM gap-fill for postings that
        # can't pass the location filter even after canton resolution (e.g. a
        # board's many foreign roles). Avoids an LLM call per clearly-foreign
        # posting that would just be dropped anyway.
        candidates = [p for p in named if could_pass_location(p, constraints)]
        logger.info("%d/%d new postings could be in range (worth LLM gap-fill)", len(candidates), len(unseen))

        # Now pay for the LLM gap-fill, but only on those candidates.
        candidates = [fill_gaps(p, client, constraints=constraints) for p in candidates]

        kept, _filter_results = filter_postings(candidates, constraints)
        logger.info("%d/%d new postings pass hard constraints", len(kept), len(unseen))

        scored = score_postings(kept, profile_block, client) if kept else []
        ranked = select_with_settings(scored, thresholds, output)
        finals = write_rationales(ranked, profile_block, client) if ranked else []

        tier_by_id = {f.scored.posting.id: f.tier for f in finals}
        surfaced_ids = set(tier_by_id)

        run_date = date.today()
        markdown_path, summary_path = write_report(
            finals,
            run_date,
            ROOT / "reports",
            degraded=degraded,
            failed_sources=failed_sources,
        )

        # Leave eligible-but-capped postings unmarked so they resurface on future
        # runs; everything else (below-floor scores, filtered out, already
        # surfaced) is marked seen. is_eligible() is the shared predicate the
        # observability outcome taxonomy also keys off, so "left to resurface"
        # and the "deferred-capped" label can't drift apart.
        eligible_not_surfaced = {
            s.posting.id
            for s in scored
            if is_eligible(s, thresholds.min_skill, thresholds.min_interest)
            and s.posting.id not in surfaced_ids
        }
        to_mark_seen = [p for p in unseen if p.id not in eligible_not_surfaced]
        seen_store.mark_seen(to_mark_seen, tier_by_id)

        # Per-run observability artifact (operator-facing, not the deliverable):
        # web_search queries, source health/funnels, stage counts, and the true
        # outcome of every scored posting. See search/observability.py.
        funnel = {
            "raw": len(raw_postings) + len(uncorroborated),
            "uncorroborated_dropped": len(uncorroborated),
            "new": len(unseen),
            "named": len(named),
            "location_candidates": len(candidates),
            "passed_hard_filters": len(kept),
            "scored": len(scored),
            "surfaced": len(finals),
        }
        run_record = build_run_record(
            run_date,
            fetch_results,
            funnel,
            scored,
            tier_by_id,
            degraded=degraded,
            failed_sources=failed_sources,
            corroboration=corroboration_counts,
            uncorroborated=uncorroborated,
            min_skill=thresholds.min_skill,
            min_interest=thresholds.min_interest,
        )
        runs_path = append_run_record(run_record, ROOT / "reports")

    logger.info("Selected %d match(es): %s", len(finals), [f.tier for f in finals])
    logger.info("Report: %s", markdown_path)
    logger.info("Summary: %s", summary_path)
    logger.info("Run log: %s", runs_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use seeded fixture postings instead of fetching live sources (still makes real LLM calls).",
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help="Skip the profile-driven web_search discovery source (faster, no Sonnet call).",
    )
    parser.add_argument(
        "--respect-schedule",
        action="store_true",
        help=(
            "Exit without running if config/search.yaml's schedule says today isn't a "
            "run day (for a local cron that fires daily). Off by default, so a run you "
            "typed always runs."
        ),
    )
    args = parser.parse_args(argv)
    try:
        run(
            dry_run=args.dry_run,
            use_web_search=not args.no_web_search,
            respect_schedule=args.respect_schedule,
        )
    except AuthenticationConfigError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

"""The per-run observability artifact: one JSON line per run, appended to
`reports/runs.jsonl`.

This is deliberately NOT part of the deliverable. The daily markdown report is
for the job-seeker (the few roles worth their time); this file is for the
operator — "is Jobradar working, and is it working *well*?" — and holds the
signals that answer that: the actual web_search query strings the model chose
(otherwise unobservable, since web_search is server-side), each source's health
and discovery funnel, the stage-by-stage funnel counts, and the true outcome of
every scored posting.

JSONL (append one line per run) rather than a per-run file, because the whole
point is trend-over-time: `jq` over the history answers "what angles has
discovery searched this month?" or "which days deferred eligible roles by the
quota?" in one pass. Like the dedup store and the reports, it's committed back
by the scheduled CI run so history survives the fresh-checkout cloud runner.

Example queries::

    # The web_search query strings from the latest run:
    tail -1 reports/runs.jsonl \\
      | jq '.sources[] | select(.name=="web_search") | .meta.queries'

    # Outcome counts across all history:
    jq -s 'map(.outcomes[].outcome) | group_by(.) | map({(.[0]): length}) | add' \\
      reports/runs.jsonl
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import DEFAULT_MIN_INTEREST, DEFAULT_MIN_SKILL
from ..models import ScoredPosting
from .ranking import classify_outcome
from .sources.base import FetchResult, RawPosting

# 2: added per-outcome `liveness`, plus the top-level `corroboration` counts and
#    `uncorroborated_drops` from the cross-source check.
# 3: added `settings` — the scoring floors the run selected with, now that they
#    are user-configurable (config/search.yaml) and so no longer implied by the
#    code at any given commit.
# 4: added per-outcome `requirements` — the checklist the skill score is now
#    computed from (matching.compute_skill_score). `skill` alone cannot be
#    audited after the fact; the checklist says which requirement cost a posting
#    its rank, quoted from the posting. This is the biggest single addition to
#    the record's size (a few hundred bytes per scored posting, and this file is
#    committed daily) — the justification is that retuning the category weights
#    needs real checklists, and without them the only corpus is the handful of
#    postings that became applications.
# 5: added `model_category` / `model_strength` to each requirement — the model's
#    own call, before the code-side checks rewrote it. `category` and `strength`
#    remain the effective values the score was computed from; without the model's
#    originals, a correction that was itself wrong would be invisible.
SCHEMA_VERSION = 5
RUNS_FILENAME = "runs.jsonl"


def _posting_outcome(
    scored: ScoredPosting,
    tier: str | None,
    min_skill: int = DEFAULT_MIN_SKILL,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> dict:
    """One scored posting's fate, with the reason kept structured so a wrongly
    capped role is diagnosable (unmet hard requirements vs. the scorer's prose).

    The floors are the run's own (config/search.yaml), not the defaults: a user
    who raised min_skill would otherwise get a log labelling postings
    "deferred-capped" that the run actually dropped below the floor.
    """
    return {
        "id": scored.posting.id,
        "title": scored.posting.title,
        "company": scored.posting.company,
        "url": scored.posting.url,
        "source": scored.posting.source,
        # What this run established about the link still being open. A run that
        # surfaced a role on an unverified link is diagnosable after the fact.
        "liveness": scored.posting.liveness,
        "skill": scored.skill_score,
        "interest": scored.interest_score,
        "outcome": classify_outcome(scored, tier, min_skill, min_interest),
        "unmet_hard_requirements": list(scored.unmet_hard_requirements),
        # The checklist behind `skill`. Verbose per posting, but it is the only
        # record of WHY a score landed where it did — and the only way to retune
        # the category weights against real postings without paying to re-score
        # them.
        "requirements": [
            {
                "quote": r.quote,
                "category": r.category,
                "strength": r.strength,
                "verdict": r.verdict,
                "evidence": r.evidence,
                "model_category": r.model_category,
                "model_strength": r.model_strength,
            }
            for r in scored.requirements
        ],
        "reason": scored.brief_reason.strip(),
    }


def build_run_record(
    run_date: date,
    fetch_results: list[FetchResult],
    funnel: dict[str, int],
    scored: list[ScoredPosting],
    tier_by_id: dict[str, str],
    *,
    degraded: bool,
    failed_sources: list[str],
    corroboration: dict[str, int] | None = None,
    uncorroborated: list[RawPosting] | None = None,
    min_skill: int = DEFAULT_MIN_SKILL,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> dict:
    """Assemble the full observability record for one run (not yet written).

    `corroboration` / `uncorroborated` come from the cross-source check (see
    search/corroborate.py). The dropped postings are listed in full rather than
    counted, because a false drop there costs a real match and is invisible
    everywhere else — the run log is the only place it can be caught.

    `min_skill` / `min_interest` are the floors this run selected with; they are
    recorded under `settings` and used to label each outcome, so a log line can
    still be read correctly after the config has been retuned.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "date": run_date.isoformat(),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "degraded": degraded,
        "failed_sources": list(failed_sources),
        "sources": [
            {
                "name": r.name,
                "ok": r.ok,
                "postings": len(r.postings),
                "detail": r.detail,
                # meta carries source-specific signal — notably web_search's
                # actual query strings and its discovery funnel.
                "meta": r.meta,
            }
            for r in fetch_results
        ],
        "funnel": dict(funnel),
        # The dials this run used (config/search.yaml). Without them, a funnel
        # count can't be told apart from the same count under a different
        # configuration, which is exactly the question a retune raises.
        "settings": {"min_skill": min_skill, "min_interest": min_interest},
        "corroboration": dict(corroboration or {}),
        "uncorroborated_drops": [
            {
                "title": p.title,
                "company": p.company,
                "url": p.url,
                "source": p.source,
                "liveness": p.liveness,
                "corroboration": p.corroboration,
            }
            for p in (uncorroborated or [])
        ],
        "outcomes": [
            _posting_outcome(s, tier_by_id.get(s.posting.id), min_skill, min_interest)
            for s in scored
        ],
    }


CORPUS_DIR_VAR = "JOBRADAR_CORPUS_DIR"


def dump_scoring_corpus(scored: list[ScoredPosting], directory: Path, run_date: date) -> Path:
    """Write the postings this run scored, descriptions included, for offline
    evaluation of the scorer.

    Off unless JOBRADAR_CORPUS_DIR is set, and deliberately env-driven rather
    than a config key: this is for a handful of manual runs when the scorer is
    being worked on, not a standing behaviour. Nothing in the pipeline reads it
    back.

    It is separate from runs.jsonl because the two have opposite lifetimes.
    runs.jsonl is committed and kept; this holds several KB of scraped posting
    text per row, which does not belong in a repository published as a
    template. Point the variable somewhere gitignored (dev/corpus/ is).

    What makes it worth having: runs.jsonl records each posting's requirement
    checklist, which is enough to retune the category weights offline for free,
    but not to re-run the model after a prompt change. That needs the
    description the model was given.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_date.isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for s in scored:
            fh.write(json.dumps({
                "id": s.posting.id,
                "url": s.posting.url,
                "title": s.posting.title,
                "company": s.posting.company,
                "location": s.posting.location_text,
                "description": s.posting.description,
                "skill": s.skill_score,
                "interest": s.interest_score,
                "unmet_hard_requirements": list(s.unmet_hard_requirements),
            }, ensure_ascii=False) + "\n")
    return path


def append_run_record(record: dict, reports_dir: Path) -> Path:
    """Append one record as a JSON line to reports/runs.jsonl, creating it if
    needed. Append-only (one line per run) keeps the git diff to a single new
    line and the history intact for trend queries.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / RUNS_FILENAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path

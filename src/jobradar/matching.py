"""Bulk skill+interest scoring against the candidate's CV(s) and identity
statement.

The CV(s) + identity statement are sent as a cached system-prompt block so
the marginal cost of scoring each additional posting in a run is small —
see build_profile_block(). Two caveats worth knowing if you're staring at
`usage.cache_read_input_tokens` and not seeing hits:

1. Caching is model-scoped — this stage uses Haiku, writeup.py uses Sonnet,
   so they write to separate caches. That's fine; each still gets reused
   across its own multiple calls within a run.
2. Haiku 4.5's minimum cacheable prefix is 4096 tokens. A short CV +
   identity statement may simply not clear that bar, in which case caching
   silently doesn't kick in (cache_creation_input_tokens stays 0) — at the
   volume this tool runs at, that's a non-issue either way.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import usage
from .llm import MODEL_DEFAULTS, model_for
from .models import Posting, ScoredPosting
# The cap is defined in terms of the eligibility floor (see _CAP_FEW_UNMET), so
# it reads the floor rather than duplicating the number. search.ranking does not
# import this module, so the dependency is one-way.
from .search.ranking import DEFAULT_MIN_SKILL

logger = logging.getLogger(__name__)

# Overridable per run via JOBRADAR_SCORING_MODEL; see jobradar.llm.
DEFAULT_SCORING_MODEL = MODEL_DEFAULTS["JOBRADAR_SCORING_MODEL"]

# Scoring is one independent API call per posting and was the single largest
# term in a run's wall clock (2026-09-08: 420 calls x ~8.4s = 59 min of a
# 95-min run), so the calls are fanned out across a small thread pool.
# Concurrency is bounded rather than unlimited because the ceiling here is the
# account's rate limit, not the runner: each call re-reads the ~10k-token
# cached profile block, so N workers put roughly N x 10k tokens/response-time
# through the input-token-per-minute budget. 8 keeps a normal day's scoring
# under a minute while staying well inside standard limits; drop it if the run
# log starts showing 429 retries.
_SCORING_WORKERS = int(os.environ.get("JOBRADAR_SCORING_WORKERS", "8"))


def load_md_dir(directory: Path) -> dict[str, str]:
    """Loads every *.md file in directory (besides README.md) keyed by stem.

    Shared by load_profile() and load_evidence() below.
    """
    if not directory.exists():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
        if path.name.lower() != "readme.md"
    }


def load_profile(
    profile_dir: Path,
) -> tuple[dict[str, str], str, dict[str, str]]:
    """Returns (cv_label -> content, identity statement text,
    story_label -> content). stories are optional and may be empty.
    """
    cvs = load_md_dir(profile_dir / "cvs")
    if not cvs:
        raise ValueError(f"No CVs found in {profile_dir / 'cvs'} (besides README.md)")

    identity_path = profile_dir / "identity.md"
    identity = identity_path.read_text(encoding="utf-8") if identity_path.exists() else ""

    stories = load_md_dir(profile_dir / "stories")

    return cvs, identity, stories


def load_evidence(profile_dir: Path) -> dict[str, str]:
    """Loads profile/evidence/ — the candidate's published technical write-ups,
    ingested from their portfolio site by jobradar.evidence.

    Deliberately NOT part of load_profile(): evidence belongs to the apply
    pipeline (a handful of Sonnet calls per posting, where the depth pays for
    itself), not to daily scoring (one Haiku call per posting per day, where
    several thousand words of technical detail would be bought over and over
    to sharpen a signal the CV's project lines already carry). Keeping it a
    separate call makes that boundary something a caller opts into rather than
    something search/main.py has to remember not to pass on.
    """
    return load_md_dir(profile_dir / "evidence")


def build_profile_block(
    cvs: dict[str, str],
    identity: str,
    stories: dict[str, str] | None = None,
    evidence: dict[str, str] | None = None,
) -> str:
    sections = [
        "# Candidate profile",
        "## Career identity, motivation, and aspirations",
        identity.strip() or "(not provided)",
    ]
    for label, content in cvs.items():
        sections.append(f"## CV: {label}")
        sections.append(content.strip())
    if stories:
        sections.append("## Interview stories (STAR)")
        sections.append(
            "First-person accounts of the candidate's key accomplishments, "
            "prepared for interviews in Situation/Task/Action/Result form. They "
            "expand on the same roles the CVs cover — they add depth and "
            "context, never employers, titles, or dates beyond the CVs. Use "
            "them as evidence of demonstrated skills when a CV bullet alone "
            "would undersell the experience, and as source material for "
            "concrete, verifiable claims when writing application documents."
        )
        for label, content in stories.items():
            sections.append(f"### Story: {label}")
            sections.append(content.strip())
    if evidence:
        sections.append("## Published technical write-ups")
        sections.append(
            "Field notes and project write-ups the candidate has PUBLISHED on "
            "their own portfolio site, ingested here in full. Each one opens "
            "with its public source URL.\n\n"
            "These differ from the interview stories above in three ways that "
            "change how you use them:\n\n"
            "- They are technical where the stories are behavioural. This is "
            "the candidate's demonstrated hands-on depth — the decisions, the "
            "trade-offs, the false starts — and it is the best evidence "
            "available that they work close to the technology rather than "
            "managing it at a distance. Weigh them accordingly against a "
            "posting's hands-on requirements.\n"
            "- They are public and verifiable. A claim grounded in one of "
            "these can be checked by anyone reading the application, so the "
            "source URL is worth citing in a cover letter when the posting "
            "makes the topic relevant.\n"
            "- They are ALREADY GENERALIZED. A write-up whose header says the "
            "details are generalized had the client, employer, or figures "
            "deliberately withheld so the public version was safe to publish. "
            "Never restore them: do not name the employer or client behind a "
            "generalized write-up, and never invent a metric a write-up chose "
            "not to give. Describe the work at the level the write-up itself "
            "does. Facts the CVs state openly stay usable, as always.\n\n"
            "They describe the same projects the CVs already list; like the "
            "stories, they add depth, never new employers, titles, or dates."
        )
        for label, content in evidence.items():
            sections.append(f"### Write-up: {label}")
            sections.append(content.strip())
    return "\n\n".join(sections)


# Only three kinds of gap are real knockouts for this candidate. The rest are
# things a tailored CV, a recruiter conversation, or simple willingness to
# interview can close, so they must not suppress the score.
#
# This used to be a prose instruction plus a lexical filter over the returned
# strings, which failed in both directions: the model ignored the instruction,
# and no regex can separate "must hold an active clearance" from "must have
# Databricks experience" — they are the same shape. Making the model tag each
# gap moves the judgement to the only place that has the context to make it,
# and leaves the policy decision here in code.
GapCategory = Literal[
    "work_eligibility",
    "licence_or_certification",
    "seniority_mismatch",
    "degree",
    "years_of_experience",
    "domain_or_industry",
    "technology",
    "other",
]

_KNOCKOUT_CATEGORIES = frozenset(
    {"work_eligibility", "licence_or_certification", "seniority_mismatch"}
)

# Applied to the gap list AFTER filtering — see _cap_skill_score.
#
# One or two knockouts land the score exactly ON the eligibility floor rather
# than under it, so the posting still surfaces (last, since combined_score
# takes the hit) and the user gets to judge the gap themselves. That matters
# because gap tagging is not perfect: a single mis-tagged requirement should
# cost a posting its ranking, not delete it from the run. Three or more
# knockouts is a pattern rather than a slip, so that drops below the floor and
# stops surfacing.
_CAP_FEW_UNMET = DEFAULT_MIN_SKILL  # 1-2 gaps: still eligible, bottom of the pile
_CAP_MANY_UNMET = 50  # 3+ gaps: below the floor, does not surface


class _Gap(BaseModel):
    requirement: str
    category: GapCategory


def _filter_gaps(gaps: list[_Gap]) -> list[str]:
    """Keeps only the gap categories that count as knockouts, returning their
    requirement text. The prompt deliberately does not say which categories
    those are, so the model has no incentive to mislabel one to make it count.
    """
    kept = []
    for gap in gaps:
        if gap.category not in _KNOCKOUT_CATEGORIES:
            logger.info("Dropping %s gap: %s", gap.category, gap.requirement)
            continue
        kept.append(gap.requirement)
    return kept


def _cap_skill_score(score: int, unmet: list[str]) -> int:
    """Caps skill_score for surviving knockout gaps.

    One or two gaps cap to the eligibility floor (still surfaces, ranked last);
    three or more cap below it (does not surface).

    The prompt used to ask the model to apply this itself, which meant a gap
    that _filter_gaps later dropped had already suppressed the score beyond
    recovery. Scoring the fit and penalising the knockouts are now separate
    steps, so the penalty is only ever applied to gaps that survived filtering.
    """
    if not unmet:
        return score
    cap = _CAP_FEW_UNMET if len(unmet) <= 2 else _CAP_MANY_UNMET
    return min(score, cap)


class _ScoreOutput(BaseModel):
    unmet_hard_requirements: list[_Gap] = Field(default_factory=list)
    skill_score: int = Field(ge=0, le=100)
    interest_score: int = Field(ge=0, le=100)
    best_cv: str
    brief_reason: str


_SCORING_PROMPT = """\
Screen this posting the way an experienced hiring manager or technical \
recruiter would on a first pass: decide whether this candidate is realistically \
worth interviewing. You are NOT a keyword-matching ATS — assume the candidate \
will tailor their CV to the posting, so credit genuine transferable and \
adjacent experience even when the CV doesn't use the posting's exact words. \
But be realistic about who actually gets shortlisted.

Work in this order:

1. Scan the posting for any language that encourages people to apply even when \
they don't meet everything — e.g. "meets several of the following", "you don't \
need to tick every box", "don't worry if you don't meet all the criteria". \
If you find any such line, leave unmet_hard_requirements EMPTY \
and go straight to scoring: the posting itself has said its requirements are \
not hard gates.

2. Otherwise, list in unmet_hard_requirements each stated requirement the \
candidate clearly does NOT meet, and tag every one with the category it \
actually belongs to:
   - work_eligibility — a legal right-to-work, visa, or residency gate. \
Location, relocation, commuting distance, travel and on-call are NOT this; \
tag those `other`.
   - licence_or_certification — a formal credential the candidate must already \
hold, such as CFA, CPA, PMP, a medical licence, or a security clearance. \
Knowledge of a framework or methodology (ITIL, SAFe, Scrum, NIST, MITRE \
ATT&CK) is `technology`, not this, even where a certification for that \
framework exists — use this category only when the posting itself names the \
credential as required, never because holding one would be the usual way to \
prove the skill.
   - seniority_mismatch — an individual contributor applying to a role whose \
core purpose is running a team, or vice versa. The role must actually centre \
on people management, not merely mention leading projects or mentoring.
   - degree — an academic qualification of any kind
   - years_of_experience — a stated minimum number of years
   - domain_or_industry — experience in a particular sector or domain
   - technology — a named tool, platform, language, framework, or stack
   - other — anything that fits none of the above
Tag by what the requirement IS, not by how serious it feels: a demand for five \
years of SOC experience is years_of_experience even when the candidate has \
none at all, and a demand for Databricks is technology even when it is listed \
as essential. Each category carries its own weight, applied automatically \
after your response — so tag accurately rather than reaching for whichever \
label sounds most severe. Credit transferable experience and the candidate's \
stated skills and languages generously as meeting a requirement.

3. Score skill_score purely on how well the candidate's skills and experience \
match what the role actually does — score it as if every requirement you listed \
in step 2 were met, and do not lower it to account for them. \
Listing a gap in unmet_hard_requirements is exactly HOW that gap gets \
penalised: each one is applied automatically to this score after your \
response, weighted by the category you gave it. A genuine knockout you leave \
out of step 2 therefore goes completely unpenalised and the posting is wrongly \
treated as a good match — so be thorough in step 2, and independent of it here.
   - 85-100: covers the core of the role and most preferred qualifications; \
a strong shortlist candidate on substance.
   - 70-84: covers the core of the role; thin on some preferred quals, but a \
tailored CV would earn an interview.
   - 55-69: a genuine stretch on several fronts; an interview is possible, \
not likely.
   - 40-54: substantially short on the skills and experience the role centres \
on.
   - 0-39: not a realistic candidate on skills and experience.

- interest_score (0-100): how well this role aligns with the candidate's \
stated career identity, motivation, and aspirations, and with the pattern \
of jobs they've previously chosen to apply to (if any are given) — \
independent of whether they're qualified.
- best_cv: which CV (by the label used in the system prompt) is the best \
fit for this specific posting.
- brief_reason: one or two sentences explaining the scores, naming the \
decisive hard-requirement gap(s) if any.

Be discriminating — most postings should NOT score highly on both axes. \
Reserve a high interest_score for postings that genuinely match what the \
candidate said they're moving toward, not just generically reasonable jobs.

Job posting:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}
"""


def score_posting(
    posting: Posting,
    profile_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
    usage_lock: threading.Lock | None = None,
) -> ScoredPosting | None:
    model = model or model_for("JOBRADAR_SCORING_MODEL")
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": profile_block,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _SCORING_PROMPT.format(
                        title=posting.title,
                        company=posting.company,
                        location=posting.location_text or "(not given)",
                        description=posting.description[:6000],
                    ),
                }
            ],
            output_format=_ScoreOutput,
        )
    except Exception as exc:  # noqa: BLE001 - scoring is best-effort per posting
        logger.warning("Scoring failed for %s: %s", posting.url, exc)
        return None

    if usage_acc is not None:
        usage.accumulate(usage_acc, response, usage_lock)

    parsed = response.parsed_output
    if parsed is None:
        return None
    unmet = _filter_gaps(parsed.unmet_hard_requirements)
    skill_score = _cap_skill_score(parsed.skill_score, unmet)
    # Surface the knockout gaps in brief_reason so they show in the report and
    # flow into the Sonnet write-up's "scoring notes" — without needing a new
    # field on ScoredPosting.
    brief_reason = parsed.brief_reason
    if unmet:
        gaps = "; ".join(unmet)
        brief_reason = f"{brief_reason} Unmet hard requirements: {gaps}."
    return ScoredPosting(
        posting=posting,
        skill_score=skill_score,
        interest_score=parsed.interest_score,
        best_cv=parsed.best_cv,
        brief_reason=brief_reason,
        unmet_hard_requirements=unmet,
    )


def score_postings(
    postings: list[Posting],
    profile_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    workers: int | None = None,
) -> list[ScoredPosting]:
    """Score every posting, fanning the calls out across a thread pool.

    Two details are load-bearing:

    1. The first posting is scored on its own before the pool starts. That call
       is what writes the cached profile block; starting the whole pool cold
       would have every worker miss the cache at once and pay to write its own
       copy, turning one cache-creation into `workers` of them.
    2. Results keep the input order (Executor.map yields in submission order,
       not completion order). search.ranking.select_matches sorts by
       combined_score with a stable sort, so input order is what breaks ties
       for the single "best" slot and the `max_okay` cut — returning results in
       completion order would make the surfaced set vary run to run.

    The client is shared: anthropic.Anthropic wraps a thread-safe httpx.Client
    and each messages.parse call is independent, so no per-thread client is
    needed. Per-posting failures are already swallowed by score_posting (it
    logs and returns None), so one bad posting can't take the pool down.
    """
    model = model or model_for("JOBRADAR_SCORING_MODEL")
    if not postings:
        return []
    workers = _SCORING_WORKERS if workers is None else workers
    usage_acc: dict = {}
    usage_lock = threading.Lock()

    def score_one(posting: Posting) -> ScoredPosting | None:
        return score_posting(
            posting, profile_block, client, model, usage_acc=usage_acc, usage_lock=usage_lock
        )

    results = [score_one(postings[0])]  # warms the prompt cache — see above
    rest = postings[1:]
    if rest:
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results.extend(pool.map(score_one, rest))
        else:
            results.extend(score_one(posting) for posting in rest)

    scored = [result for result in results if result is not None]
    # score_posting swallows its own failures (logging one line each) and
    # returns None, so a rate-limited burst would otherwise show up only as a
    # quietly shorter list -- and running `workers` calls at once is exactly
    # what makes a 429 burst likely. One summary line makes the loss countable
    # against the funnel's passed_hard_filters in runs.jsonl.
    dropped = len(results) - len(scored)
    if dropped:
        logger.warning("Scoring dropped %d/%d postings (see warnings above)", dropped, len(results))
    usage.log_summary(logger, "scoring", usage_acc)
    return scored

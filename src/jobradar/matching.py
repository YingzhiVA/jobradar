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
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import usage
from .cv import sync_word_cvs
from .llm import MODEL_DEFAULTS, model_for
from .models import Posting, RequirementAssessment, ScoredPosting
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
    outcomes = sync_word_cvs(profile_dir / "cvs")
    for outcome in outcomes:
        log = logger.warning if outcome.status in ("failed", "kept_edits") else logger.info
        log("CV: %s", outcome.message)
    cvs = load_md_dir(profile_dir / "cvs")
    if not cvs:
        failed = "; ".join(o.message for o in outcomes if o.status == "failed")
        raise ValueError(
            f"No CVs found in {profile_dir / 'cvs'} (besides README.md)" + (f": {failed}" if failed else "")
        )

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


# The taxonomy the model tags each requirement with, and what each tag costs.
#
# This used to be a prose instruction plus a lexical filter over the returned
# strings, which failed in both directions: the model ignored the instruction,
# and no regex can separate "must hold an active clearance" from "must have
# Databricks experience" — they are the same shape. Making the model tag each
# requirement moves the judgement to the only place that has the context to
# make it, and leaves the policy decision here in code.
#
# `language_requirement` is separate from `other` because a mandatory working
# language is a genuine gate that no tailored CV closes, and lumping it into
# `other` meant a posting demanding "zwingend fliessend Deutsch" carried no
# penalty at all.
RequirementCategory = Literal[
    "work_eligibility",
    "licence_or_certification",
    "seniority_mismatch",
    "language_requirement",
    "degree",
    "years_of_experience",
    "domain_or_industry",
    "technology",
    "other",
]

# Gates: an unmet must-have here is not something a tailored CV, a recruiter
# conversation, or willingness to interview can close, so it caps the score
# outright rather than just weighing on it.
_GATE_CATEGORIES = frozenset(
    {
        "work_eligibility",
        "licence_or_certification",
        "seniority_mismatch",
        "language_requirement",
    }
)

# How much each requirement counts in the weighted mean below. This is what the
# prompt means when it says a requirement is weighted by its category — before,
# the prompt claimed that while the code did a binary keep/drop, and five of the
# eight categories were discarded outright.
#
# `degree` is deliberately low rather than zero: measuring against this
# candidate's own history, degree requirements were the most frequently
# MIS-flagged — an MSc in Biomedical Engineering read as failing "MSc in
# Computer Science, Mathematics, or similar" four separate times — so a degree
# gap should tilt a score, not decide it. `other` is zero: it is the bucket for
# everything the taxonomy has no opinion about (travel, commute, on-call), and
# those are the candidate's call, not the scorer's.
# Gate categories are deliberately absent: they are eligibility facts, not
# degrees of fit. Holding a work permit says nothing about how well someone
# would do the job, and lacking one is not a shortfall to be averaged away — it
# is handled by the gate rule instead. Counting a gate here as well would also
# double-penalise it, dragging a posting under the floor that the cap means to
# land exactly ON, which is the difference between "you decide" and "you never
# see it".
_CATEGORY_WEIGHT: dict[str, float] = {
    "years_of_experience": 1.0,
    "domain_or_industry": 1.0,
    "technology": 1.0,
    "degree": 0.25,
    "other": 0.0,
}

_VERDICT_CREDIT: dict[str, float] = {"met": 1.0, "partial": 0.5, "unmet": 0.0}

# How the two halves of the checklist combine. Must-haves carry the score;
# preferred qualifications are what separates a candidate who clears the bar
# from one who clears it comfortably, which is roughly the gap between the
# rubric's 70-84 and 85-100 bands.
_CORE_WEIGHT = 0.8
_EXTRA_WEIGHT = 0.2

# One or two unmet gates land the score exactly ON the eligibility floor rather
# than under it, so the posting still surfaces (last, since combined_score takes
# the hit) and the user gets to judge the gate themselves. That matters because
# tagging is not perfect: a single mis-tagged requirement should cost a posting
# its ranking, not delete it from the run. Three or more is a pattern rather
# than a slip, so that drops below the floor and stops surfacing.
_CAP_FEW_UNMET = DEFAULT_MIN_SKILL  # 1-2 gates: still eligible, bottom of the pile
_CAP_MANY_UNMET = 50  # 3+ gates: below the floor, does not surface

# Words that mark a stated requirement as optional, in the languages Swiss
# postings actually use. Surveying this candidate's 32 applications, explicit
# HARD markers barely exist — "must have" appeared in 2 postings of 32,
# "essential" and "non-negotiable" in none — while softeners appear in 16 and
# 12 respectively, and only 9 of 32 postings separate required from preferred
# with section headings at all. So the detectable signal is the softener, and
# the rule is: a stated requirement is a must-have unless something says
# otherwise.
#
# The same list is quoted into the prompt (see _SCORING_PROMPT) so the model's
# instruction and this check cannot drift apart.
# Each entry is (what to call it in the prompt, how to find it in a quote).
# Patterns rather than plain substrings because intensifiers are the common
# case and a literal match misses them: the line that motivated this check,
# "Clinical trials, drug development, or health data experience is a strong
# plus", does not contain the string "a plus".
_SOFTENERS: tuple[tuple[str, str], ...] = (
    ("preferred", r"preferred"),
    ("preferably", r"preferably"),
    ("ideally", r"ideally"),
    ("nice to have", r"nice[\s-]to[\s-]have"),
    ("a plus", r"\ba (?:\w+ )?plus\b"),
    ("a bonus", r"\ba (?:\w+ )?bonus\b"),
    ("an asset", r"\ban (?:\w+ )?asset\b"),
    ("beneficial", r"beneficial"),
    ("advantageous", r"advantageous"),
    ("desirable", r"desirable"),
    ("not required", r"not\s+required"),
    ("optional", r"\boptional\b"),
    ("von Vorteil", r"von\s+(?:\w+\s+)?Vorteil"),        # German: "an advantage"
    ("wünschenswert", r"w(?:ü|ue)nschenswert"),          # German: "desirable"
    ("idealerweise", r"idealerweise"),                   # German: "ideally"
    ("un atout", r"un\s+(?:\w+\s+)?atout"),              # French: "an asset"
    ("souhaité", r"souhait(?:é|e)"),                     # French: "desired"
    ("apprécié", r"appr(?:é|e)ci(?:é|e)"),               # French: "appreciated"
)

_SOFTENER_RE = re.compile(
    "|".join(pattern for _, pattern in _SOFTENERS), re.IGNORECASE
)

# The same list, rendered for the prompt. Built from the tuple above rather than
# written out again, so the instruction and the check cannot drift.
_SOFTENER_LIST = ", ".join(f'"{label}"' for label, _ in _SOFTENERS)


def _looks_optional(quote: str) -> bool:
    """Whether a requirement's own wording marks it optional.

    A belt-and-braces check on the model's `strength` call, and the reason
    `quote` must be verbatim: measured against this candidate's history, 23% of
    the requirements the scorer flagged as gaps were sitting on a line the
    posting had explicitly softened ("...is a strong plus"), which under the
    old design was harmless only because the category filter discarded them
    anyway. It cannot see a softener that lives in a section heading rather than
    the line itself — that stays the model's job, since only it sees the
    heading.
    """
    return bool(_SOFTENER_RE.search(quote))


class _Requirement(BaseModel):
    quote: str
    category: RequirementCategory
    strength: Literal["must_have", "preferred"]
    verdict: Literal["met", "partial", "unmet"]
    evidence: str = ""


def effective_strength(requirement: _Requirement) -> str:
    """The requirement's strength after the code-side softener check.

    Never promotes: the model may downgrade a requirement this check would have
    left alone (it can see section headings and blanket "you don't need to tick
    every box" language, which a per-line regex cannot), and that judgement
    stands.
    """
    if requirement.strength == "must_have" and _looks_optional(requirement.quote):
        logger.info("Softened wording downgrades to preferred: %s", requirement.quote)
        return "preferred"
    return requirement.strength


# What a genuine line-management demand looks like in a quote, English and
# German, and what the category definition says it is NOT. Together they gate
# `seniority_mismatch`: see effective_category.
#
# The prompt already tells the model that leading a project, a workstream or a
# cross-functional team is not this category. Measured over the first two runs
# of the checklist scorer, it tagged such lines anyway — 13 of 25 decided
# seniority tags were wrong, and 8 of those 13 were cases the prompt excludes by
# name. The model keys on the word "lead", so the definition has to be enforced
# here rather than only stated there. The other failures were level mismatches
# ("Trainee", "in your penultimate year of study" — the category's name invites
# them) and the reporting line pointing the wrong way ("Reporting to the
# Portfolio Lead"). None of those carries a line-management marker, so all fail
# the check below.
#
# The windows ([^.;]{0,80}) exist only to stop a marker pairing across unrelated
# clauses; the clause boundary is the real guard. German puts the object late
# ("Du führst, entwickelst und coachst ein engagiertes Team"), hence the width.
_LINE_MANAGEMENT_RE = re.compile(
    r"people[\s-]manag"
    r"|direct\s+reports?"
    r"|\bhir(?:e|ing)\b"
    r"|performance\s+manag"
    r"|\bmanag(?:e|es|ing)\b[^.;]{0,80}\bteams?\b"
    r"|\bteam\s+lead\b"
    r"|lead(?:ing)?\s+and\s+(?:manag|develop)\w*[^.;]{0,30}\bteams?\b"
    r"|\blead\s+a\s+(?:dedicated|small|global)?\s*\w*\s*team\b"
    r"|accountab\w*\s+for\s+[^.;]{0,40}organi[sz]ations?"
    r"|personelle\s+Führung|Führungserfahrung|führst[^.;]{0,80}Team"
    r"|Mitarbeitende\s+führen",
    re.IGNORECASE,
)
_NOT_LINE_MANAGEMENT_RE = re.compile(
    r"reporting\s+to"
    r"|cross[\s-]functional|cross[\s-]team|matrix"
    r"|\bprojects?\b|\bprogram(?:me)?s?\b|workstreams?|transformations?",
    re.IGNORECASE,
)


def _shows_line_management(quote: str) -> bool:
    """Whether a quote demands managing people, as opposed to leading work.

    A veto wins over a marker: "leadership experience, for example by leading
    projects, technical workstreams or serving as a (deputy) team lead" names a
    team lead, but it is satisfiable by leading a project, so it is not a demand
    for line management.
    """
    return bool(_LINE_MANAGEMENT_RE.search(quote)) and not _NOT_LINE_MANAGEMENT_RE.search(quote)


def effective_category(requirement: _Requirement) -> str:
    """The requirement's category after the code-side line-management check.

    A `seniority_mismatch` whose own wording does not show line management is
    treated as `other` — ignored, neither gating nor weighing on fit. That is the
    recall-biased reading: a line the model mis-tagged is not trusted to cap a
    posting, and an ambiguous one ("lead and empower a small squad") is given
    the benefit of the doubt. Every other category passes through unchanged.
    """
    if requirement.category == "seniority_mismatch" and not _shows_line_management(
        requirement.quote
    ):
        logger.info("Not a line-management demand, ignored as a gate: %s", requirement.quote)
        return "other"
    return requirement.category


def unmet_gates(requirements: list[_Requirement]) -> list[str]:
    """The unmet must-have requirements in a gate category, as quoted text.

    These are what cap the score, and what the report and the apply pipeline
    show as the posting's hard blockers.
    """
    return [
        r.quote
        for r in requirements
        if effective_strength(r) == "must_have"
        and r.verdict == "unmet"
        and effective_category(r) in _GATE_CATEGORIES
    ]


def _weighted_credit(
    requirements: list[_Requirement], weights: dict[str, float] | None = None
) -> float | None:
    """Weighted mean of met/partial/unmet credit, or None if nothing counts.

    None (rather than 0.0) when every requirement in the group has zero weight,
    so the caller can tell "the candidate meets none of these" apart from "there
    was nothing here to meet". A category missing from _CATEGORY_WEIGHT weighs
    nothing — that is how the gate categories stay out of the mean.
    """
    weights = _CATEGORY_WEIGHT if weights is None else weights
    total = sum(weights.get(effective_category(r), 0.0) for r in requirements)
    if total == 0:
        return None
    earned = sum(
        weights.get(effective_category(r), 0.0) * _VERDICT_CREDIT[r.verdict]
        for r in requirements
    )
    return earned / total


def compute_skill_score(
    requirements: list[_Requirement], weights: dict[str, float] | None = None
) -> tuple[int, list[str]]:
    """The skill score and the unmet gates behind it.

    The model no longer produces a 0-100 number. It produces a checklist, and
    the number is computed here, for two reasons. Measured over this candidate's
    32 applications, the free-floating score had collapsed into a single band —
    76% of scoring calls returned exactly 72 or 78, nothing fell below the
    eligibility floor, and the resulting ranking separated applications that
    cleared CV screening from those that did not no better than shuffling them.
    A score derived from the checklist gets its range back by construction. And
    because the model call and this function are separate, the weights above can
    be retuned against stored checklists without spending a single API call —
    `weights` overrides _CATEGORY_WEIGHT for exactly that, so the tuning tool
    replays the real algorithm rather than a copy of it that can drift.

    An empty checklist scores at the floor rather than failing. A posting that
    fails to score is not merely skipped: search/main.py marks it seen, so it
    never comes back. Surfacing an unscoreable posting last, for the user to
    judge, is the recall-biased choice this pipeline makes everywhere else.
    """
    if not requirements:
        logger.warning("Scorer returned no requirements; scoring at the floor")
        return DEFAULT_MIN_SKILL, []

    core = _weighted_credit(
        [r for r in requirements if effective_strength(r) == "must_have"], weights
    )
    extra = _weighted_credit(
        [r for r in requirements if effective_strength(r) == "preferred"], weights
    )
    # A posting with no must-haves (everything softened, or everything tagged
    # `other`) is judged on what it does state; one with no preferred
    # qualifications is judged on its must-haves alone rather than being
    # penalised 20 points for not listing any.
    if core is None and extra is None:
        logger.warning("Scorer returned no weighted requirements; scoring at the floor")
        return DEFAULT_MIN_SKILL, []
    if core is None:
        fit = extra
    elif extra is None:
        fit = core
    else:
        fit = _CORE_WEIGHT * core + _EXTRA_WEIGHT * extra

    score = round(100 * fit)
    gates = unmet_gates(requirements)
    if gates:
        cap = _CAP_FEW_UNMET if len(gates) <= 2 else _CAP_MANY_UNMET
        score = min(score, cap)
    return score, gates


class _ScoreOutput(BaseModel):
    # No default. A default here is silently dangerous: the model omitted this
    # field on 12% of draws while still writing a confident brief_reason, and
    # `default_factory=list` turned every one of those into an empty checklist
    # that scored at the floor — a bug wearing a plausible number. Required, it
    # is a validation error instead, which the retry in score_posting handles.
    requirements: list[_Requirement]
    interest_score: int = Field(ge=0, le=100)
    best_cv: str
    brief_reason: str


# How much of a posting the scorer sees. The score is computed from the
# requirements the model finds, so a cut that lands mid-qualifications silently
# turns "the candidate meets everything stated" into "nothing was stated" —
# measured over this candidate's own applications, 7 of 32 postings had their
# requirements section severed at the old 6000-character limit. Haiku input is
# cheap enough that the headroom costs a few cents a day.
DESCRIPTION_LIMIT = 12000

# A checklist costs more output than a single number did: the old schema
# averaged ~230 output tokens per call. The ceiling is set well above what a
# well-behaved response needs, because running out of room here is not a
# degraded answer but a total loss: the response is cut mid-JSON, fails to
# parse, and score_posting returns None. At 4000 that still happened on 3 of
# 160 calls — twice on a long posting, once on a response that degenerated into
# repeated newlines — so the ceiling is set where a runaway, not a normal
# answer, is what hits it. The ceiling is not billed, only the tokens actually
# produced (a well-behaved response runs about 900), and the prompt caps quote
# and evidence length to keep it there.
_MAX_OUTPUT_TOKENS = 8000

_SCORING_PROMPT = """\
Screen this posting the way an experienced hiring manager or technical \
recruiter would on a first pass. You are NOT a keyword-matching ATS — assume \
the candidate will tailor their CV to the posting, so credit genuine \
transferable and adjacent experience even when the CV doesn't use the \
posting's exact words. But be realistic about who actually gets shortlisted.

Work through the posting and return one entry per requirement it states. Do \
not invent requirements the posting does not make, and do not merge two \
separate demands into one entry. Equally, give each requirement ONE entry: if \
a single demand could fit two categories ("5 years in strategy consulting" is \
both a duration and a field), pick the one that captures what would actually \
disqualify a candidate, and list it once.

List only requirements a recruiter could actually screen a CV against: \
concrete, checkable things like a named technology, a field of experience, a \
number of years, a qualification, a language. **Leave out generic professional \
qualities** — communication, analytical thinking, problem-solving, teamwork, \
stakeholder management, being proactive or curious — unless the posting ties \
one to something specific and checkable. Nearly every posting asks for these, \
nearly every candidate claims them, and listing them buries the requirements \
that actually decide the screen. At most 10 entries, and fewer when the \
posting states fewer. Skip boilerplate about company values and benefits.

For each requirement give:

1. `quote` — the requirement WORD FOR WORD from the posting. Copy the text, do \
not paraphrase, summarise or translate it. Quote the requirement itself, not \
the sentence around it, and keep it under 25 words — if a bullet is longer \
than that, quote the part that states the requirement. This is checked against \
the posting afterwards.

2. `category` — what the requirement IS, not how serious it feels:
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
   - seniority_mismatch — a demand for LINE-MANAGEMENT experience: direct \
reports, hiring, performance-managing, or running a team as its manager. A \
requirement naming direct reports belongs HERE, not in years_of_experience, \
however it is phrased — "3+ years managing a team" is seniority_mismatch, not \
a years requirement. The test is whether people would report to this person. \
Leading a project, an engagement or a workstream is NOT this, and neither is \
mentoring, coaching, guiding a squad, or "leading cross-functional teams" \
without authority over them — those are ordinary senior-IC work. If the \
posting does not say the role manages people, do not use this category.
   - language_requirement — working fluency in a named human language.
   - degree — an academic qualification of any kind.
   - years_of_experience — a stated minimum number of years.
   - domain_or_industry — experience in a particular sector or domain.
   - technology — a named tool, platform, language, framework, or stack.
   - other — anything that fits none of the above.
   A demand for five years of SOC experience is years_of_experience even when \
the candidate has none at all, and a demand for Databricks is technology even \
when it is listed as essential. Each category carries its own weight, applied \
automatically after your response — so tag accurately rather than reaching for \
whichever label sounds most severe.

3. `strength` — `must_have` or `preferred`. **Default to must_have.** A stated \
requirement is a must-have unless the posting says otherwise. Downgrade to \
`preferred` only when there is an explicit signal:
   - the requirement's own wording softens it: {softeners};
   - it sits under a heading that does, such as "Preferred qualifications", \
"Nice to have", "Bonus", "A plus";
   - or the posting tells applicants they need not meet everything — \
"meets several of the following", "you don't need to tick every box", \
"don't worry if you don't meet all the criteria". That language downgrades the \
qualifications it refers to, but it never downgrades a legal right-to-work \
gate, a required licence, or a mandatory working language: a posting cannot \
encourage its way out of those.
   Most postings do not separate required from preferred at all. A plain \
bullet under "Your Profile", "What You'll Bring" or "Deine Skills", with no \
softening word, is a must_have.

4. `verdict` — `met`, `partial`, or `unmet`, judging the candidate against it. \
Credit transferable experience and the candidate's stated skills, degrees and \
languages generously. Use `partial` when there is real adjacent evidence that \
falls short of what is asked — the right kind of work at a smaller scale, or \
the skill without the years. Use `unmet` when there is nothing to point to.
   **Judge the requirement whole.** Most are compound: a duration AND a \
setting ("5-10 years in a leading management consultancy"), a skill AND a \
scale ("MLOps at enterprise scale"), a field AND a seniority. It is `met` only \
when every part of it is. Satisfying one part and not another is `partial` at \
best, and `unmet` when the part that is missing is the substance of the \
requirement — a candidate with twelve years of product management and none of \
it in consulting does not meet "5-10 years in a leading management \
consultancy", however many years they have. Read the whole phrase before \
deciding, not the first half of it.

5. `evidence` — what in the profile meets it, or what is missing. **At most 12 \
words.** A pointer, not an argument: "12 years product management", "no \
PySpark anywhere in profile".

Then give:

- interest_score (0-100): how well this role aligns with the candidate's \
stated career identity, motivation, and aspirations — independent of whether \
they're qualified. Be discriminating: reserve a high score for postings that \
genuinely match what the candidate said they're moving toward, not just \
generically reasonable jobs.
- best_cv: which CV (by the label used in the system prompt) is the best fit \
for this specific posting.
- brief_reason: one or two sentences on the fit, naming the decisive unmet \
requirement(s) if any. Two sentences at most — this is a note, not a report.

Do NOT return an overall skill score. It is computed from your checklist.

Job posting:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}
"""


# One retry per posting. Two failure modes need it, and neither is a reason to
# write a posting off: a transient API error, and a response that never closes
# its JSON — the model occasionally runs away (one observed draw produced 32k
# characters of mostly repeated whitespace before hitting the ceiling, which no
# affordable ceiling would have contained). Both are stochastic, so a second
# draw almost always lands. Measured over 160 calls, the first attempt failed
# 6 times: 3 API 500s and 3 runaways.
_SCORING_ATTEMPTS = 2


def _score_once(
    posting: Posting,
    profile_block: str,
    client: anthropic.Anthropic,
    model: str,
    usage_acc: dict | None,
    usage_lock: threading.Lock | None,
) -> _ScoreOutput | None:
    """One scoring call. Returns None if it failed or came back unparseable.

    Usage is accumulated even for a call that fails to parse: the tokens were
    produced and billed, and a runaway response is exactly the kind of cost that
    should not be invisible in the run's token summary.
    """
    response = client.messages.parse(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
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
                    softeners=_SOFTENER_LIST,
                    title=posting.title,
                    company=posting.company,
                    location=posting.location_text or "(not given)",
                    description=posting.description[:DESCRIPTION_LIMIT],
                ),
            }
        ],
        output_format=_ScoreOutput,
    )
    if usage_acc is not None:
        usage.accumulate(usage_acc, response, usage_lock)
    parsed = response.parsed_output
    if parsed is not None and not parsed.requirements:
        # Every real posting states something screenable, so an empty list is a
        # failed read rather than a finding. Treated as a failed attempt so the
        # retry gets a second draw; compute_skill_score still has its own floor
        # fallback for the case where both draws come back empty.
        logger.warning("Scorer returned an empty checklist for %s", posting.url)
        return None
    return parsed


def score_posting(
    posting: Posting,
    profile_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
    usage_lock: threading.Lock | None = None,
) -> ScoredPosting | None:
    model = model or model_for("JOBRADAR_SCORING_MODEL")
    parsed = None
    for attempt in range(1, _SCORING_ATTEMPTS + 1):
        try:
            parsed = _score_once(
                posting, profile_block, client, model, usage_acc, usage_lock
            )
        except Exception as exc:  # noqa: BLE001 - scoring is best-effort per posting
            logger.warning(
                "Scoring attempt %d/%d failed for %s: %s",
                attempt, _SCORING_ATTEMPTS, posting.url, exc,
            )
            continue
        if parsed is not None:
            break
        logger.warning(
            "Scoring attempt %d/%d returned nothing usable for %s",
            attempt, _SCORING_ATTEMPTS, posting.url,
        )
    if parsed is None:
        return None

    skill_score, gates = compute_skill_score(parsed.requirements)
    # Surface the gates in brief_reason so they show in the report and flow into
    # the Sonnet write-up's "scoring notes" — without needing a new field on
    # ScoredPosting.
    brief_reason = parsed.brief_reason
    if gates:
        brief_reason = f"{brief_reason} Unmet hard requirements: {'; '.join(gates)}."
    return ScoredPosting(
        posting=posting,
        skill_score=skill_score,
        interest_score=parsed.interest_score,
        best_cv=parsed.best_cv,
        brief_reason=brief_reason,
        unmet_hard_requirements=gates,
        requirements=[
            RequirementAssessment(
                quote=r.quote,
                category=effective_category(r),
                strength=effective_strength(r),
                verdict=r.verdict,
                evidence=r.evidence,
                model_category=r.category,
                model_strength=r.strength,
            )
            for r in parsed.requirements
        ],
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

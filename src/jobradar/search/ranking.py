"""Selects what actually gets surfaced: a small number of "best" matches plus
a few "okay" ones (by default 1 best + 3 okay = the ~4/day the user actually
reviews), with an empty result being a perfectly valid outcome (e.g. on a quiet
day with nothing worth showing).

Every dial here — the two floors, the "best" bar and both caps — is user-owned
and lives in `config/search.yaml` (see `jobradar.config`). The defaults below
are the fallback for a missing file/key; `search/main.py` reads the settings
once per run and passes them in, so nothing here should be edited to retune a
search.

Eligibility gates on SKILL ONLY by default — a posting surfaces if it clears
the skill floor, regardless of interest. This is deliberate and recall-biased
for a thin market: a high-skill / low-interest role the user is genuinely
qualified for is still worth a look, and for anyone who must meet a monthly
application quota (Swiss RAV registrants, for instance) it is a legitimate,
defensible application even if it isn't what they're aiming for. Interest is
not discarded — it still feeds combined_score, so it decides *ordering* and
whether the top pick clears the "best" bar: a low-interest role can surface as
"okay" but effectively can't become the headline "best", since that needs a
high combined score (interest ~70+).

`thresholds.min_interest` in the config turns interest into a second gate for a
user who'd rather see fewer, more motivating roles; it is 0 (off) by default,
which keeps the behaviour above exactly as described.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import (
    DEFAULT_BEST_THRESHOLD,
    DEFAULT_MAX_BEST,
    DEFAULT_MAX_OKAY,
    DEFAULT_MIN_INTEREST,
    DEFAULT_MIN_SKILL,
    Output,
    Thresholds,
)
from ..models import ScoredPosting

# Re-exported from jobradar.config, which owns the defaults so the YAML, the
# dataclasses and the docs can't drift. Kept under their original names here
# because they read as this module's own vocabulary at the call sites.
__all__ = [
    "DEFAULT_BEST_THRESHOLD",
    "DEFAULT_MAX_BEST",
    "DEFAULT_MAX_OKAY",
    "DEFAULT_MIN_INTEREST",
    "DEFAULT_MIN_SKILL",
    "OUTCOME_BELOW_FLOOR",
    "OUTCOME_BEST",
    "OUTCOME_DEFERRED",
    "OUTCOME_OKAY",
    "OUTCOME_UNMET_HARD",
    "RankedPosting",
    "classify_outcome",
    "NEAR_FLOOR_BAND",
    "ids_to_leave_unmarked",
    "is_eligible",
    "near_floor_ids",
    "select_matches",
    "select_with_settings",
]

# Per-posting outcome labels for observability. These name the true fate of each
# scored posting and, crucially, split the two categories the old report-facing
# "dropped" list conflated: a posting that cleared the skill floor but lost the
# best/okay quota (DEFERRED — left unmarked, resurfaces next run) is NOT
# the same as one that fell below the floor (BELOW_FLOOR — marked seen, gone).
OUTCOME_BEST = "surfaced-best"
OUTCOME_OKAY = "surfaced-okay"
OUTCOME_DEFERRED = "deferred-capped"
OUTCOME_UNMET_HARD = "unmet-hard-requirement"
OUTCOME_BELOW_FLOOR = "below-floor"


@dataclass
class RankedPosting:
    scored: ScoredPosting
    tier: str  # "best" | "okay"


def is_eligible(
    scored: ScoredPosting,
    min_skill: int = DEFAULT_MIN_SKILL,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> bool:
    """Whether a scored posting clears the configured floor(s) — the single
    source of truth for "eligible". select_matches gates on this, main.py's
    resurface decision keys off it (eligible-but-unsurfaced postings are left
    unmarked so they come back), and the outcome taxonomy below reuses it, so
    the three can never drift apart.

    `min_interest` defaults to 0, i.e. interest does not gate (see the module
    docstring); it only becomes a second floor if the user raises it.
    """
    return scored.skill_score >= min_skill and scored.interest_score >= min_interest


def classify_outcome(
    scored: ScoredPosting,
    tier: str | None,
    min_skill: int = DEFAULT_MIN_SKILL,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> str:
    """The observability label for one scored posting given whether it surfaced.

    `tier` is "best"/"okay" if it surfaced, else None. Precedence puts DEFERRED
    ahead of the below-floor reasons so the label stays truthful to the dedup
    behavior: DEFERRED ⟺ eligible-but-unsurfaced ⟺ left unmarked to resurface.

    The floors must be the same ones the run selected with, or the log would
    describe a different run than the one that happened — main.py passes the
    configured values through observability.build_run_record for exactly that
    reason.
    """
    if tier == "best":
        return OUTCOME_BEST
    if tier == "okay":
        return OUTCOME_OKAY
    if is_eligible(scored, min_skill, min_interest):
        return OUTCOME_DEFERRED
    if scored.unmet_hard_requirements:
        return OUTCOME_UNMET_HARD
    return OUTCOME_BELOW_FLOOR


# How far below the floor still counts as "might have cleared it on another
# draw", and how many times such a posting is scored before the run takes its
# answer as final (the first scoring plus two more).
#
# Scoring a posting is not deterministic, and since the score was restructured
# around a requirement checklist it is markedly less repeatable than it was:
# identical draws of the same posting move by about 16 points, and 7 of the 32
# applications measured in dev/scoring-eval/ fell below the floor on at least
# one draw while sitting above it on median. The two errors are not symmetric.
# A posting wrongly kept costs one more scoring call; a posting wrongly dropped
# is marked seen and never returns. So a near miss gets another draw on another
# day rather than a verdict from one unlucky one.
#
# Bounded deliberately: left to resurface indefinitely, a posting parked just
# under the floor would be re-fetched, re-gap-filled and re-scored every day
# for the weeks it stays open, which is a standing cost for an answer the run
# has already given three times.
NEAR_FLOOR_BAND = 10
NEAR_FLOOR_ATTEMPTS = 3


def near_floor_ids(
    scored: list[ScoredPosting],
    min_skill: int = DEFAULT_MIN_SKILL,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> set[str]:
    """Postings that missed the floor narrowly enough to deserve another draw.

    Only the skill axis has the repeatability problem, so a posting held back by
    an interest floor the user set is not given extra chances — its score is not
    what is in doubt.
    """
    return {
        s.posting.id
        for s in scored
        if not is_eligible(s, min_skill, min_interest)
        and s.interest_score >= min_interest
        and min_skill - NEAR_FLOOR_BAND <= s.skill_score < min_skill
    }


def ids_to_leave_unmarked(
    candidate_ids: set[str],
    scored: list[ScoredPosting],
    surfaced_ids: set[str],
    min_skill: int = DEFAULT_MIN_SKILL,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> set[str]:
    """Postings that must NOT be marked seen, so a later run scores them again.

    Two distinct reasons, kept together because both mean "come back tomorrow"
    and separating them is how they drift:

    - **Eligible but unsurfaced.** It cleared the floor and lost the daily
      quota. This is OUTCOME_DEFERRED, and is_eligible() is the shared predicate
      so the log label and the resurface decision cannot disagree.
    - **Never scored.** It reached the scorer and no verdict came back — a
      timeout, a rate limit, a response that overran its token budget mid-JSON.
      That is not the same as scoring badly, and marking it seen would discard a
      posting permanently on the strength of an infrastructure hiccup. Scoring
      is best-effort per posting (matching.score_posting logs and returns None),
      which only stays safe if the loss is temporary.

    `candidate_ids` is everything handed to the scorer; anything in it with no
    entry in `scored` is taken to have failed.
    """
    deferred = {
        s.posting.id
        for s in scored
        if is_eligible(s, min_skill, min_interest) and s.posting.id not in surfaced_ids
    }
    never_scored = candidate_ids - {s.posting.id for s in scored}
    return deferred | never_scored


def select_matches(
    scored: list[ScoredPosting],
    best_threshold: int = DEFAULT_BEST_THRESHOLD,
    min_skill: int = DEFAULT_MIN_SKILL,
    max_okay: int = DEFAULT_MAX_OKAY,
    max_best: int = DEFAULT_MAX_BEST,
    min_interest: int = DEFAULT_MIN_INTEREST,
) -> list[RankedPosting]:
    """The day's selection: up to `max_best` headline picks, then up to
    `max_okay` secondary ones, ordered by combined score throughout.

    A posting can only take a "best" slot by clearing `best_threshold`; unused
    best slots are not handed down as extra okay slots, so raising max_best
    widens the headline without quietly widening the whole report.
    """
    # Gate on skill (and on interest only if the user set a floor); interest
    # still orders via combined_score, so a high-skill/low-interest role
    # surfaces (as "okay") but a genuine high-both fit still ranks first and
    # can claim a "best" slot.
    eligible = [s for s in scored if is_eligible(s, min_skill, min_interest)]
    remaining = sorted(eligible, key=lambda s: s.combined_score, reverse=True)
    selected: list[RankedPosting] = []

    while remaining and len(selected) < max_best and remaining[0].combined_score >= best_threshold:
        selected.append(RankedPosting(scored=remaining.pop(0), tier="best"))

    selected.extend(RankedPosting(scored=s, tier="okay") for s in remaining[:max_okay])

    return selected


def select_with_settings(
    scored: list[ScoredPosting], thresholds: Thresholds, output: Output
) -> list[RankedPosting]:
    """select_matches driven straight off the config dataclasses — the form
    the pipeline uses, so a new dial can't be added to the YAML and forgotten
    at the one call site that matters.
    """
    return select_matches(
        scored,
        best_threshold=thresholds.best_threshold,
        min_skill=thresholds.min_skill,
        max_okay=output.max_okay,
        max_best=output.max_best,
        min_interest=thresholds.min_interest,
    )

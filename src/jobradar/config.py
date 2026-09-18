"""The user-facing dials for steering the search: ``config/search.yaml``.

Everything here is a *preference* — how much to surface, how good a match has
to be, how often to look. That is deliberately a different kind of setting
from its neighbours in ``config/``:

- ``constraints.yaml`` holds hard, non-negotiable requirements (deterministic
  pass/fail filters — a posting that fails one is dropped, full stop).
- ``companies.yaml`` holds *where* to look.
- ``retention.yaml`` holds how long output is kept around afterwards.
- ``search.yaml`` (this module) holds how the search itself is steered, plus
  two situation switches the phases read: ``rav`` (Swiss unemployment-office
  reporting, for registered job seekers only) and ``sources`` (optional
  sources that need no per-company config).

Values were previously module constants spread across ``search/ranking.py``
and a cron line in ``.github/workflows/daily.yml``, i.e. editable only by
changing code. They now live in one file the user owns, and the pipeline reads
them from here.

Loading follows ``archive.load_retention``'s contract — a missing file or a
missing key falls back to the dataclass default, so deleting the file (or any
key in it) is always safe. Unlike that one, a *present but invalid* value is a
hard error (``ConfigError``) rather than a silent fallback: these dials change
what the search surfaces, so a typo'd threshold quietly reverting to 60 would
mean days of subtly wrong results with nothing to notice. Failing on the first
run puts the mistake in front of the user while they still remember making it.

Usage::

    python -m jobradar.config      # print the effective settings and exit
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "search.yaml"

# Defaults, kept here (not in the modules that consume them) so the file, the
# dataclasses and the documentation can't drift apart. search/ranking.py
# re-exports the ranking ones under its old DEFAULT_* names.
DEFAULT_MIN_SKILL = 60
DEFAULT_MIN_INTEREST = 0  # 0 = interest does not gate; see Thresholds.min_interest
DEFAULT_BEST_THRESHOLD = 80
DEFAULT_MAX_BEST = 1
DEFAULT_MAX_OKAY = 3
DEFAULT_FREQUENCY = "daily"
DEFAULT_DAYS_OF_WEEK = ("mon", "tue", "wed", "thu", "fri")

# jobs.ethz.ch "Stellentyp" ids known to the ETH board connector. Other
# categories exist; their ids are read off the search form's dropdown source.
ETH_JOB_TYPES: dict[str, int] = {"management": 4, "administration": 5}
DEFAULT_ETH_JOB_TYPES: tuple[int, ...] = (
    ETH_JOB_TYPES["management"],
    ETH_JOB_TYPES["administration"],
)

_TRUE_WORDS = {"true", "yes", "on", "1"}
_FALSE_WORDS = {"false", "no", "off", "0"}

# Named cadences -> the minimum whole days between two runs. "custom" defers to
# the schedule's own min_interval_days.
FREQUENCY_INTERVALS: dict[str, int] = {
    "daily": 1,
    "every_other_day": 2,
    "twice_weekly": 3,
    "weekly": 7,
}
CUSTOM_FREQUENCY = "custom"

# Canonical three-letter weekday keys, indexed by date.weekday() (Mon == 0).
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_ALIASES = {
    **{day: day for day in WEEKDAYS},
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}


class ConfigError(ValueError):
    """A value in config/search.yaml is present but unusable."""


def _int_in_range(value: object, *, key: str, low: int, high: int | None = None) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: expected a whole number, got {value!r}") from exc
    if number < low or (high is not None and number > high):
        bound = f"{low}-{high}" if high is not None else f"{low} or more"
        raise ConfigError(f"{key}: expected {bound}, got {number}")
    return number


def _bool(value: object, *, key: str) -> bool:
    """A real YAML bool, or one of the usual words for people who quote them."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise ConfigError(f"{key}: expected true or false, got {value!r}")


def _unknown_keys(data: dict, known: set[str], *, section: str) -> None:
    """Warn (don't fail) on keys we don't recognise.

    A stray key is most likely a comment-turned-setting or a rename we made, and
    neither is worth refusing to run a day's search over — but it IS worth
    saying out loud, since the user edited it expecting an effect.
    """
    for key in data:
        if key not in known:
            logger.warning(
                "config/%s: ignoring unknown key %r in %s", CONFIG_FILENAME, key, section
            )


@dataclass(frozen=True)
class Thresholds:
    """How good a posting has to be, on the scorer's two 0-100 axes."""

    # The skill floor: below this, a posting is dropped and marked seen (it
    # will not come back). This is the one gate that decides eligibility.
    min_skill: int = DEFAULT_MIN_SKILL
    # The interest ("motivation") floor. 0 by default, i.e. OFF — see
    # search/ranking.py's docstring: gating on skill alone is deliberate, so
    # that a role the user is plainly qualified for but lukewarm about still
    # surfaces (recall matters in a thin market, and anyone who must meet a
    # monthly application quota, e.g. Swiss RAV registrants, needs those too).
    # Raise it to make motivation gate as well, at the cost of surfacing fewer
    # applyable roles on a thin day.
    min_interest: int = DEFAULT_MIN_INTEREST
    # The bar for the headline "best" tier, applied to combined_score (the mean
    # of the two axes). A posting that clears min_skill but not this surfaces
    # as "okay" instead.
    best_threshold: int = DEFAULT_BEST_THRESHOLD

    @classmethod
    def from_dict(cls, data: dict | None) -> "Thresholds":
        data = data or {}
        _unknown_keys(
            data, {"min_skill", "min_interest", "best_threshold"}, section="thresholds"
        )
        defaults = cls()
        return cls(
            min_skill=_int_in_range(
                data.get("min_skill", defaults.min_skill),
                key="thresholds.min_skill",
                low=0,
                high=100,
            ),
            min_interest=_int_in_range(
                data.get("min_interest", defaults.min_interest),
                key="thresholds.min_interest",
                low=0,
                high=100,
            ),
            best_threshold=_int_in_range(
                data.get("best_threshold", defaults.best_threshold),
                key="thresholds.best_threshold",
                low=0,
                high=100,
            ),
        )


@dataclass(frozen=True)
class Output:
    """How many matches one run is allowed to surface."""

    # Headline matches (those clearing best_threshold). 1 by default.
    max_best: int = DEFAULT_MAX_BEST
    # Secondary matches, i.e. eligible ones that didn't clear best_threshold or
    # lost the best slots. The daily total is max_best + max_okay.
    max_okay: int = DEFAULT_MAX_OKAY

    @classmethod
    def from_dict(cls, data: dict | None) -> "Output":
        data = data or {}
        _unknown_keys(data, {"max_best", "max_okay"}, section="output")
        defaults = cls()
        # 0 is allowed for either (e.g. max_best: 0 to never promote a headline
        # pick), but not for both — that would process every posting and then
        # surface nothing, which is never what someone meant to configure.
        max_best = _int_in_range(data.get("max_best", defaults.max_best), key="output.max_best", low=0)
        max_okay = _int_in_range(data.get("max_okay", defaults.max_okay), key="output.max_okay", low=0)
        if max_best == 0 and max_okay == 0:
            raise ConfigError(
                "output.max_best and output.max_okay are both 0 — a run would score "
                "postings and surface nothing. Set at least one above 0."
            )
        return cls(max_best=max_best, max_okay=max_okay)


@dataclass(frozen=True)
class Schedule:
    """How often the search actually runs.

    The cadence is expressed as a minimum interval plus an allowed set of
    weekdays, rather than as a cron line, because a GitHub Actions `cron:`
    can't be read from a config file — the workflow fires every day and
    jobradar.schedule decides whether that day is a run day. An interval
    measured against the *last completed run* (rather than a modulo on the
    calendar) also self-heals: if GitHub drops a Wednesday run, Thursday's is
    already due, instead of the cadence staying stuck on the wrong parity.
    """

    frequency: str = DEFAULT_FREQUENCY
    # Only consulted when frequency is "custom".
    min_interval_days: int = 1
    # Days the search may run at all. Empty tuple means any day.
    days_of_week: tuple[str, ...] = DEFAULT_DAYS_OF_WEEK

    @property
    def interval_days(self) -> int:
        """Minimum whole days between two runs, resolved from `frequency`."""
        if self.frequency == CUSTOM_FREQUENCY:
            return self.min_interval_days
        return FREQUENCY_INTERVALS[self.frequency]

    def allows_weekday(self, weekday: int) -> bool:
        """Whether a `date.weekday()` (Mon == 0) is an allowed run day."""
        return not self.days_of_week or WEEKDAYS[weekday] in self.days_of_week

    @classmethod
    def from_dict(cls, data: dict | None) -> "Schedule":
        data = data or {}
        _unknown_keys(data, {"frequency", "min_interval_days", "days_of_week"}, section="schedule")
        defaults = cls()

        frequency = str(data.get("frequency", defaults.frequency)).strip().lower()
        allowed = sorted(FREQUENCY_INTERVALS) + [CUSTOM_FREQUENCY]
        if frequency not in allowed:
            raise ConfigError(
                f"schedule.frequency: {frequency!r} is not one of {', '.join(allowed)}"
            )

        min_interval_days = _int_in_range(
            data.get("min_interval_days", defaults.min_interval_days),
            key="schedule.min_interval_days",
            low=1,
        )

        raw_days = data.get("days_of_week", list(defaults.days_of_week))
        if raw_days is None:
            raw_days = []
        if isinstance(raw_days, str) or not isinstance(raw_days, (list, tuple)):
            raise ConfigError(
                "schedule.days_of_week: expected a list of weekday names, e.g. [mon, wed, fri]"
            )
        days: list[str] = []
        for raw in raw_days:
            # PyYAML has no "sun"/"sat" surprises, but an unquoted `on`/`off`
            # would arrive as a bool, so stringify before matching.
            key = str(raw).strip().lower()
            canonical = _WEEKDAY_ALIASES.get(key)
            if canonical is None:
                raise ConfigError(
                    f"schedule.days_of_week: {raw!r} is not a weekday "
                    f"(use {', '.join(WEEKDAYS)})"
                )
            if canonical not in days:
                days.append(canonical)
        # Keep calendar order regardless of how they were listed, so logs and
        # `python -m jobradar.config` read the way a week does.
        days.sort(key=WEEKDAYS.index)

        return cls(
            frequency=frequency,
            min_interval_days=min_interval_days,
            days_of_week=tuple(days),
        )


@dataclass(frozen=True)
class Rav:
    """Swiss RAV (regional unemployment office) reporting.

    Registered job seekers must hand in a monthly proof-of-applications table
    («Nachweis der persönlichen Arbeitsbemühungen»). When enabled, the apply
    CLI renders it (``--rav``), records it as filed (``--rav-filed``), and keeps
    a submitted application out of the archive until the month it was
    submitted in has been filed. Off by default: most job seekers are not
    registered, and for them the archive should follow retention.yaml alone.
    """

    enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> "Rav":
        data = data or {}
        _unknown_keys(data, {"enabled"}, section="rav")
        return cls(enabled=_bool(data.get("enabled", cls().enabled), key="rav.enabled"))


@dataclass(frozen=True)
class EthSource:
    """The jobs.ethz.ch board, scraped per job-type category.

    Which categories are relevant depends entirely on the user's role, so the
    source is off until they pick some; ``job_types`` accepts the board's
    numeric ids or the names in ``ETH_JOB_TYPES``.
    """

    enabled: bool = False
    job_types: tuple[int, ...] = DEFAULT_ETH_JOB_TYPES

    @classmethod
    def from_dict(cls, data: dict | None) -> "EthSource":
        data = data or {}
        _unknown_keys(data, {"enabled", "job_types"}, section="sources.eth_jobs")
        defaults = cls()
        enabled = _bool(data.get("enabled", defaults.enabled), key="sources.eth_jobs.enabled")

        raw_types = data.get("job_types", list(defaults.job_types))
        if raw_types is None:
            raw_types = []
        if isinstance(raw_types, str) or not isinstance(raw_types, (list, tuple)):
            raise ConfigError(
                "sources.eth_jobs.job_types: expected a list of job-type ids or names, "
                "e.g. [management, administration]"
            )
        job_types: list[int] = []
        for raw in raw_types:
            if isinstance(raw, bool):  # a bare `on`/`off` in YAML; never a job type
                raise ConfigError(f"sources.eth_jobs.job_types: {raw!r} is not a job type")
            if isinstance(raw, int):
                job_type = _int_in_range(raw, key="sources.eth_jobs.job_types", low=1)
            else:
                name = str(raw).strip().lower()
                if name not in ETH_JOB_TYPES:
                    raise ConfigError(
                        f"sources.eth_jobs.job_types: {raw!r} is not a jobs.ethz.ch job-type "
                        f"id or one of {', '.join(sorted(ETH_JOB_TYPES))}"
                    )
                job_type = ETH_JOB_TYPES[name]
            if job_type not in job_types:
                job_types.append(job_type)
        if enabled and not job_types:
            raise ConfigError(
                "sources.eth_jobs is enabled but job_types is empty — list at least one "
                "category, or set enabled: false"
            )
        return cls(enabled=enabled, job_types=tuple(job_types))


@dataclass(frozen=True)
class Sources:
    """Sources that need no per-company configuration and can be switched on
    or off here. The ATS boards in companies.yaml and the profile-driven web
    search are always on."""

    eth_jobs: EthSource = EthSource()

    @classmethod
    def from_dict(cls, data: dict | None) -> "Sources":
        data = data or {}
        _unknown_keys(data, {"eth_jobs"}, section="sources")
        return cls(eth_jobs=EthSource.from_dict(data.get("eth_jobs")))


@dataclass(frozen=True)
class SearchSettings:
    thresholds: Thresholds = Thresholds()
    output: Output = Output()
    schedule: Schedule = Schedule()
    rav: Rav = Rav()
    sources: Sources = Sources()

    @classmethod
    def from_dict(cls, data: dict | None) -> "SearchSettings":
        data = data or {}
        _unknown_keys(
            data,
            {"thresholds", "output", "schedule", "rav", "sources"},
            section="the top level",
        )
        return cls(
            thresholds=Thresholds.from_dict(data.get("thresholds")),
            output=Output.from_dict(data.get("output")),
            schedule=Schedule.from_dict(data.get("schedule")),
            rav=Rav.from_dict(data.get("rav")),
            sources=Sources.from_dict(data.get("sources")),
        )

    def describe(self) -> str:
        """The effective settings as a short human-readable block."""
        s = self.schedule
        cadence = s.frequency
        if s.frequency == CUSTOM_FREQUENCY:
            cadence += f" (every {s.interval_days} day{'s' if s.interval_days != 1 else ''})"
        days = ", ".join(s.days_of_week) if s.days_of_week else "any day"
        eth = self.sources.eth_jobs
        return "\n".join(
            [
                "thresholds:",
                f"  min_skill:       {self.thresholds.min_skill}",
                f"  min_interest:    {self.thresholds.min_interest}"
                + ("  (off — interest does not gate)" if not self.thresholds.min_interest else ""),
                f"  best_threshold:  {self.thresholds.best_threshold}",
                "output:",
                f"  max_best:        {self.output.max_best}",
                f"  max_okay:        {self.output.max_okay}",
                f"  (up to {self.output.max_best + self.output.max_okay} match(es) per run)",
                "schedule:",
                f"  frequency:       {cadence} — at least {s.interval_days} day(s) between runs",
                f"  days_of_week:    {days}",
                "rav:",
                f"  enabled:         {'true' if self.rav.enabled else 'false'}"
                + (
                    ""
                    if self.rav.enabled
                    else "  (--rav / --rav-filed disabled; archiving ignores the filed-month gate)"
                ),
                "sources:",
                "  eth_jobs:        "
                + (
                    f"on (job types {', '.join(str(t) for t in eth.job_types)})"
                    if eth.enabled
                    else "off"
                ),
            ]
        )


def load_search_settings(root: Path) -> SearchSettings:
    """Read config/search.yaml, falling back per-key to the dataclass defaults.

    Raises ConfigError if the file exists but holds an unusable value.
    """
    path = root / "config" / CONFIG_FILENAME
    if not path.exists():
        logger.info("No %s — using built-in defaults.", path)
        return SearchSettings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping of sections, got {type(data).__name__}")
    try:
        return SearchSettings.from_dict(data)
    except ConfigError as exc:
        # Re-raise with the file named, so the message is actionable on its own
        # in a CI log where nothing else says which file is meant.
        raise ConfigError(f"{path}: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Print the effective search settings (config/search.yaml merged over the defaults)."
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    root = Path(__file__).resolve().parents[2]
    try:
        settings = load_search_settings(root)
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    print(settings.describe())


# `replace` is re-exported for callers that want to override one dial in tests
# or a one-off run without hand-rebuilding the whole nested structure.
__all__ = [
    "ConfigError",
    "ETH_JOB_TYPES",
    "EthSource",
    "Output",
    "Rav",
    "Schedule",
    "SearchSettings",
    "Sources",
    "Thresholds",
    "load_search_settings",
    "replace",
]


if __name__ == "__main__":
    main()

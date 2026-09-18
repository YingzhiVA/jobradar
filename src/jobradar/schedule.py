"""Decides whether today is a run day, per ``schedule:`` in config/search.yaml.

Why this exists as code rather than as a cron line: a GitHub Actions `cron:`
is static — it cannot read a value out of a config file, and the user's dial
lives in a config file. So the workflow keeps firing every day and asks this
module whether to go ahead; `.github/workflows/daily.yml` calls
``python -m jobradar.schedule --check``, which writes ``skip=true|false`` to
``$GITHUB_OUTPUT`` and gates every downstream step (pipeline, email, archive,
commit). Locally, ``jobradar.search.main --respect-schedule`` asks the same
question through the same function, so a machine-local cron behaves the same.

The cadence is "at least N days since the last completed run", measured against
``reports/runs.jsonl`` — the run ledger the scheduled job commits back. Two
reasons for that rather than a modulo on the calendar:

- It self-heals. GitHub drops scheduled runs outright (see daily.yml's
  backstop comment); with a modulo, a dropped Wednesday on an every-other-day
  cadence means waiting until Friday. Here, Thursday is simply already due.
- It has no anchor date to keep in sync. Changing the cadence in the YAML takes
  effect from the last run, with nothing else to update.

``runs.jsonl`` is the ledger rather than ``reports/YYYY-MM-DD.md`` for the same
reason daily.yml's own guard prefers it: the retention sweep relocates dated
reports into ``reports/archive/`` but never touches runs.jsonl.

This is a cadence gate only. It deliberately does NOT re-implement daily.yml's
"has today's run already landed?" guard — that one protects against a double
run on the same day and stays where it is; with any interval >= 1 day this gate
happens to cover the same case, but the two answer different questions and are
kept separate so neither is load-bearing for the other.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import ConfigError, Schedule, load_search_settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
RUNS_FILENAME = "runs.jsonl"


@dataclass(frozen=True)
class Decision:
    run: bool
    reason: str


def last_run_date(reports_dir: Path) -> date | None:
    """The date of the most recent recorded run, or None if there is none.

    Reads the whole ledger rather than just the last line: records are appended
    in run order, but a rebase or a manual edit can leave them out of date
    order, and taking the max is both cheap (one line per run) and immune to
    that. A corrupt or dateless line is skipped, never fatal — a malformed
    ledger must not be able to stop the search from running.
    """
    path = reports_dir / RUNS_FILENAME
    if not path.exists():
        return None
    latest: date | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            recorded = date.fromisoformat(record["date"])
        except (ValueError, TypeError, KeyError):
            continue
        if latest is None or recorded > latest:
            latest = recorded
    return latest


def decide(today: date, last_run: date | None, schedule: Schedule) -> Decision:
    """Whether to run on `today`, given when the last run was.

    Weekday first, then the interval: a day that isn't a run day at all is a
    plain "not today", regardless of how overdue the cadence is.
    """
    if not schedule.allows_weekday(today.weekday()):
        days = ", ".join(schedule.days_of_week)
        return Decision(
            False,
            f"{today.strftime('%A')} is not a configured run day (days_of_week: {days})",
        )

    interval = schedule.interval_days
    if last_run is None:
        return Decision(True, "no previous run recorded")

    elapsed = (today - last_run).days
    if elapsed < 0:
        # The ledger is ahead of today — a clock skew, a hand-edited record, or
        # a re-run of an older checkout. Refusing would strand the schedule
        # indefinitely, so run and say why it looked odd.
        return Decision(
            True, f"last recorded run is in the future ({last_run.isoformat()}); running anyway"
        )
    if elapsed < interval:
        return Decision(
            False,
            f"last run was {last_run.isoformat()} ({elapsed} day(s) ago); "
            f"frequency '{schedule.frequency}' wants at least {interval}",
        )
    return Decision(
        True,
        f"last run was {last_run.isoformat()} ({elapsed} day(s) ago); "
        f"frequency '{schedule.frequency}' wants at least {interval}",
    )


def should_run_today(root: Path, today: date | None = None) -> Decision:
    """The cadence decision for a checkout, reading both config and ledger."""
    settings = load_search_settings(root)
    return decide(
        today or date.today(),
        last_run_date(root / "reports"),
        settings.schedule,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print the decision (and write skip=true|false to $GITHUB_OUTPUT when set).",
    )
    parser.add_argument(
        "--date",
        help="Evaluate against this date (YYYY-MM-DD) instead of today — useful for previewing.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    today = date.fromisoformat(args.date) if args.date else date.today()
    try:
        decision = should_run_today(ROOT, today)
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    verdict = "RUN" if decision.run else "SKIP"
    logger.info("%s %s — %s", today.isoformat(), verdict, decision.reason)

    # Exit 0 either way: "not a run day" is a normal outcome, not a failure.
    # The answer travels as a step output so the workflow can gate on it.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"skip={'false' if decision.run else 'true'}\n")
            fh.write(f"reason={decision.reason}\n")


if __name__ == "__main__":
    main()

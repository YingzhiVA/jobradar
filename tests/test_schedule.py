import json
from datetime import date

from jobradar.config import Schedule
from jobradar.schedule import decide, last_run_date, should_run_today

# 2026-09-14 is a Monday; 2026-09-19 a Saturday.
MON = date(2026, 9, 14)
TUE = date(2026, 9, 15)
WED = date(2026, 9, 16)
SAT = date(2026, 9, 19)

EVERY_DAY = Schedule(days_of_week=())


def write_runs(reports_dir, dates):
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "runs.jsonl"
    path.write_text(
        "".join(json.dumps({"date": d, "schema_version": 3}) + "\n" for d in dates),
        encoding="utf-8",
    )
    return path


def test_no_previous_run_always_runs():
    assert decide(MON, None, Schedule()).run is True


def test_daily_skips_a_day_that_already_ran():
    assert decide(MON, MON, Schedule()).run is False


def test_daily_runs_the_next_day():
    assert decide(TUE, MON, Schedule()).run is True


def test_every_other_day_skips_the_day_after():
    schedule = Schedule(frequency="every_other_day")
    assert decide(TUE, MON, schedule).run is False
    assert decide(WED, MON, schedule).run is True


def test_weekly_waits_seven_days():
    schedule = Schedule(frequency="weekly")
    assert decide(date(2026, 9, 20), MON, schedule).run is False
    assert decide(date(2026, 9, 21), MON, schedule).run is True


def test_custom_frequency_uses_its_interval():
    schedule = Schedule(frequency="custom", min_interval_days=3, days_of_week=())
    assert decide(WED, MON, schedule).run is False
    assert decide(date(2026, 9, 17), MON, schedule).run is True


def test_dropped_run_self_heals_instead_of_shifting_the_cadence():
    # The reason the interval is measured from the last run rather than a
    # calendar modulo: GitHub drops scheduled runs, and an every-other-day
    # cadence must not then sit out an extra day waiting for the right parity.
    schedule = Schedule(frequency="every_other_day")
    # Ran Monday, Wednesday's run never fired -> Thursday is already due.
    assert decide(date(2026, 9, 17), MON, schedule).run is True


def test_weekday_not_in_days_of_week_is_skipped_however_overdue():
    assert decide(SAT, MON, Schedule()).run is False
    assert "Saturday" in decide(SAT, MON, Schedule()).reason


def test_empty_days_of_week_allows_the_weekend():
    assert decide(SAT, None, EVERY_DAY).run is True


def test_future_last_run_still_runs():
    # A clock skew or hand-edited ledger must not strand the schedule forever.
    decision = decide(MON, date(2026, 9, 20), EVERY_DAY)
    assert decision.run is True
    assert "future" in decision.reason


def test_reason_is_always_populated():
    for decision in (
        decide(MON, None, Schedule()),
        decide(MON, MON, Schedule()),
        decide(SAT, MON, Schedule()),
        decide(TUE, MON, Schedule()),
    ):
        assert decision.reason


def test_last_run_date_reads_the_ledger(tmp_path):
    write_runs(tmp_path / "reports", ["2026-09-14", "2026-09-15", "2026-09-16"])
    assert last_run_date(tmp_path / "reports") == WED


def test_last_run_date_takes_the_max_not_the_last_line(tmp_path):
    # Records are appended in run order, but a rebase or hand edit can leave
    # them out of date order.
    write_runs(tmp_path / "reports", ["2026-09-16", "2026-09-14"])
    assert last_run_date(tmp_path / "reports") == WED


def test_last_run_date_without_ledger_is_none(tmp_path):
    assert last_run_date(tmp_path / "reports") is None


def test_corrupt_ledger_lines_are_skipped(tmp_path):
    # A malformed ledger must not be able to stop the search from running.
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "runs.jsonl").write_text(
        "\n".join(
            [
                "not json at all",
                json.dumps({"no_date_key": True}),
                json.dumps({"date": "not-a-date"}),
                json.dumps({"date": "2026-09-15"}),
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert last_run_date(reports) == TUE


def test_should_run_today_combines_config_and_ledger(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "search.yaml").write_text(
        "schedule:\n  frequency: every_other_day\n  days_of_week: [mon, tue, wed, thu, fri]\n",
        encoding="utf-8",
    )
    write_runs(tmp_path / "reports", ["2026-09-14"])
    assert should_run_today(tmp_path, TUE).run is False
    assert should_run_today(tmp_path, WED).run is True


def test_should_run_today_with_no_config_uses_defaults(tmp_path):
    assert should_run_today(tmp_path, MON).run is True

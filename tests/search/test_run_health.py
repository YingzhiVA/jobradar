import json
import logging
from datetime import date

from jobradar.apply.findings import parse_report
from jobradar.search import run_health
from jobradar.search.notify import build_message, should_send
from jobradar.search.observability import build_run_record
from jobradar.search.report import render_markdown, render_summary
from jobradar.search.run_health import (
    MIN_HISTORY,
    NEW,
    PERSISTENT,
    SPIKE,
    Flag,
    WarningCounter,
    assess,
    counting_warnings,
    load_history,
    subject_of,
)

_SCORING = "Scoring attempt %d/%d returned nothing usable for %s"
_LEGARTIS = "https://legartis.jobs.personio.de/job/2885065"


def _warning(template=_SCORING, count=2, subjects=None, logger="jobradar.matching"):
    return {
        "logger": logger,
        "template": template,
        "level": "WARNING",
        "count": count,
        "subjects": {_LEGARTIS: count} if subjects is None else subjects,
        "example": "Scoring attempt 2/2 returned nothing usable for " + _LEGARTIS,
    }


def _quiet_run():
    return [_warning("Skipping %s (%s): %s", 1, {"Optiml": 1}, "jobradar.search.sources.company_pages")]


# --- what a warning is about ---------------------------------------------------


def test_the_subject_is_a_url_first_then_a_host_then_short_text():
    assert subject_of((1, 2, _LEGARTIS)) == _LEGARTIS
    assert subject_of((429, "GET", "jj.wd5.myworkdayjobs.com", "headers: …\n body: …")) == (
        "jj.wd5.myworkdayjobs.com"
    )
    assert subject_of(("Optiml", "join", "Client error '404 Not Found'")) == "Optiml"
    assert subject_of((1, 2)) is None
    assert subject_of(()) is None
    assert subject_of(("line one\nline two",)) is None


# --- collecting ---------------------------------------------------------------


def test_warnings_are_counted_by_template_with_their_subjects():
    counter = WarningCounter()
    log = logging.getLogger("jobradar.matching")
    log.addHandler(counter)
    try:
        for attempt in (1, 2):
            log.warning(_SCORING, attempt, 2, _LEGARTIS)
        log.warning(_SCORING, 1, 2, "https://unique.jobs.personio.de/job/1")
        log.info("Scored %d postings", 40)  # below WARNING: not counted
    finally:
        log.removeHandler(counter)
    [entry] = counter.snapshot()
    assert entry["template"] == _SCORING and entry["logger"] == "jobradar.matching"
    assert entry["count"] == 3
    assert entry["subjects"] == {_LEGARTIS: 2, "https://unique.jobs.personio.de/job/1": 1}
    assert entry["example"] == f"Scoring attempt 1/2 returned nothing usable for {_LEGARTIS}"


def test_main_logs_under_its_module_name_however_it_was_started():
    counter = WarningCounter()
    counter.emit(logging.LogRecord("__main__", logging.WARNING, "", 0, "Run is degraded — %s", ("eth",), None))
    assert counter.snapshot()[0]["logger"] == "jobradar.search.main"


def test_subjects_are_capped_but_still_counted(monkeypatch):
    monkeypatch.setattr(run_health, "MAX_SUBJECTS", 2)
    counter = WarningCounter()
    for n in range(4):
        counter.emit(logging.LogRecord("x", logging.WARNING, "", 0, "gone: %s", (f"https://a.ch/{n}",), None))
    [entry] = counter.snapshot()
    assert entry["count"] == 4 and len(entry["subjects"]) == 2


def test_the_counter_is_on_the_root_logger_only_inside_the_block():
    with counting_warnings() as counter:
        logging.getLogger("jobradar.anything").warning("inside %s", "x")
    logging.getLogger("jobradar.anything").warning("outside %s", "x")
    assert [e["template"] for e in counter.snapshot()] == ["inside %s"]
    assert counter not in logging.getLogger().handlers


# --- comparing ----------------------------------------------------------------


def test_a_subject_on_three_runs_in_a_row_is_persistent_and_emailed_once():
    # The Legartis loop: the same posting failing scoring on every run.
    third = assess([_warning()], [[_warning()], [_warning()]])
    assert [(f.kind, f.runs, f.subjects, f.email) for f in third] == [
        (PERSISTENT, 3, [_LEGARTIS], True)
    ]
    fourth = assess([_warning()], [[_warning()]] * 3)
    assert [(f.kind, f.runs, f.email) for f in fourth] == [(PERSISTENT, 4, False)]
    assert assess([_warning()], [[_warning()]]) == []  # two runs: not yet


def test_a_run_without_the_subject_or_without_a_health_record_breaks_the_streak():
    other = _warning(subjects={"https://other.ch/job": 1})
    assert assess([_warning()], [[_warning()], [other], [_warning()]]) == []
    assert assess([_warning()], [[_warning()], None, [_warning()]]) == []


def test_a_template_none_of_the_earlier_runs_logged_is_new():
    history = [_quiet_run()] * MIN_HISTORY
    [flag] = assess([_warning()], history)
    assert (flag.kind, flag.email, flag.subjects) == (NEW, True, [_LEGARTIS])


def test_nothing_is_new_until_enough_runs_have_a_health_record():
    # The first runs after this went in would otherwise flag every warning.
    history = [_quiet_run()] * (MIN_HISTORY - 1) + [None] * 10
    assert assess([_warning()], history) == []


def test_a_template_far_above_its_usual_count_is_a_spike_and_not_emailed():
    usual = _warning("%s %s from %s; the response:\n  %s", 2, {}, "jobradar.search.sources.backoff")
    today = {**usual, "count": 40}
    # On every other run, so not also persistent; usual = median(2, 0, 2, 0, 2).
    history = [[usual], [], [usual], [], [usual]]
    [flag] = assess([today], history)
    assert (flag.kind, flag.count, flag.usual, flag.email) == (SPIKE, 40, 2, False)
    assert assess([{**usual, "count": 6}], history) == []  # 3x, but only 4 more


def test_a_warning_with_no_subject_on_every_run_is_persistent_as_a_whole():
    bare = _warning("Scoring dropped %d/%d postings (see warnings above)", 1, {})
    [flag] = assess([bare], [[bare], [bare]])
    assert (flag.kind, flag.runs, flag.subjects) == (PERSISTENT, 3, [])


def test_load_history_reads_the_latest_runs_first(tmp_path):
    runs = tmp_path / "runs.jsonl"
    lines = [
        json.dumps({"date": "2026-11-24"}),  # before health was recorded
        "not json",
        json.dumps({"date": "2026-11-25", "health": {"warnings": [_warning(count=1)]}}),
        json.dumps({"date": "2026-11-26", "health": {"warnings": [_warning(count=2)]}}),
    ]
    runs.write_text("\n".join(lines) + "\n", encoding="utf-8")
    history = load_history(runs)
    assert [h[0]["count"] if h else None for h in history] == [2, 1, None, None]
    assert load_history(runs, limit=1) == [[_warning(count=2)]]
    assert load_history(tmp_path / "missing.jsonl") == []


# --- surfacing ----------------------------------------------------------------


def _flags():
    return [
        Flag(NEW, "jobradar.search.liveness", "liveness: %s answered 403; keeping it as unverified",
             1, "liveness: https://www.personio.com/ answered 403", ["https://www.personio.com/"], email=True),
        Flag(PERSISTENT, "jobradar.matching", _SCORING, 2, "Scoring attempt 2/2 …", [_LEGARTIS], runs=3, email=True),
        Flag(SPIKE, "jobradar.search.sources.backoff", "Giving up on retries for %s", 30, "", usual=1.0),
    ]


def test_the_report_gets_a_run_health_section_only_when_something_is_flagged():
    quiet = render_markdown([], date(2026, 11, 26))
    assert "Run health" not in quiet
    report = render_markdown([], date(2026, 11, 26), health=_flags())
    assert "## Run health" in report
    assert "**New:** `liveness: %s answered 403; keeping it as unverified`" in report
    assert f"**3 runs in a row:** `{_SCORING}` (jobradar.matching) for `{_LEGARTIS}`" in report
    assert "**More than usual:** `Giving up on retries for %s`" in report and "usually 1." in report


def test_the_run_health_section_ends_the_last_match_for_the_report_parser():
    text = (
        "# Job matches — 2026-11-26\n\n## [BEST] PM — Acme\n\n- Link: https://acme.ch/1\n\nWrite-up.\n\n"
        + "\n".join(render_markdown([], date(2026, 11, 26), health=_flags()).splitlines()[2:])
    )
    [finding] = parse_report(text, "2026-11-26")
    assert "Run health" not in finding.writeup and "Scoring attempt" not in finding.writeup


def test_the_email_names_new_and_newly_persistent_warnings_but_not_spikes():
    summary = render_summary([], date(2026, 11, 26), health=_flags())
    assert should_send(summary)  # a quiet day, but worth an interruption
    _, body = build_message(summary)
    assert "New: liveness: %s answered 403; keeping it as unverified (1 time)" in body
    assert f"3 runs in a row: {_SCORING} (for {_LEGARTIS})" in body
    assert "Giving up on retries" not in body


def test_a_spike_alone_does_not_send_the_email():
    summary = render_summary([], date(2026, 11, 26), health=[_flags()[2]])
    assert not should_send(summary)


def test_the_run_record_carries_the_warning_counts():
    record = build_run_record(
        date(2026, 11, 26), [], {}, [], {}, degraded=False, failed_sources=[],
        health_warnings=[_warning()],
    )
    assert record["schema_version"] == 7
    assert record["health"] == {"warnings": [_warning()]}

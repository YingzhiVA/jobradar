"""A practice run leaves no trace in real state, and a second real run on the
same day adds to the day's report instead of replacing it."""

import json
import logging
from types import SimpleNamespace
from datetime import date, datetime

import anthropic
import pytest

from jobradar.apply.findings import load_findings
from jobradar.models import FinalPosting, Posting, ScoredPosting
from jobradar.schedule import is_practice_run, last_run_date
from jobradar.search import main as search_main
from jobradar.search.observability import write_practice_record
from jobradar.search.report import PRACTICE_REPORT, source_label, write_report
from jobradar.search.sources.base import FetchResult
from jobradar.ui import views

DAY = date(2026, 11, 18)


def _final(url: str, title: str, tier: str = "best", source: str = "lever") -> FinalPosting:
    posting = Posting(
        id=url, source=source, url=url, title=title, company="Acme",
        description="d", location_text="Zurich",
    )
    scored = ScoredPosting(
        posting=posting, skill_score=80, interest_score=70, best_cv="cv", brief_reason="",
    )
    return FinalPosting(scored=scored, tier=tier, writeup=f"Why {title} fits.")


# ── same-day runs ────────────────────────────────────────────────────────────


def test_a_second_run_the_same_day_adds_to_the_report(tmp_path):
    reports = tmp_path / "reports"
    write_report([_final("https://a.example/1", "First")], DAY, reports)
    write_report(
        [_final("https://a.example/2", "Second", tier="okay")], DAY, reports,
        run_at=datetime(2026, 11, 18, 14, 5),
    )
    text = (reports / "2026-11-18.md").read_text(encoding="utf-8")
    assert "## Later run at 14:05" in text

    matches = views.parse_report(text)
    assert [m["title"] for m in matches] == ["First", "Second"]
    # The later run's heading must not leak into the first run's write-up.
    assert matches[0]["writeup"] == "Why First fits."

    findings = load_findings(reports)
    assert findings["https://a.example/1"].writeup == "Why First fits."
    assert findings["https://a.example/2"].tier == "okay"

    # latest.json is the latest run only: the daily email is about that run.
    latest = json.loads((reports / "latest.json").read_text(encoding="utf-8"))
    assert [m["title"] for m in latest["matches"]] == ["Second"]


def test_a_quiet_later_run_says_so_without_hiding_the_first(tmp_path):
    reports = tmp_path / "reports"
    write_report([_final("https://a.example/1", "First")], DAY, reports)
    write_report([], DAY, reports, run_at=datetime(2026, 11, 18, 9, 30))
    text = (reports / "2026-11-18.md").read_text(encoding="utf-8")
    assert "No new matches in this run." in text
    assert [m["title"] for m in views.parse_report(text)] == ["First"]


# ── where a match came from ──────────────────────────────────────────────────


def test_each_match_says_where_it_was_found(tmp_path):
    reports = tmp_path / "reports"
    write_report(
        [_final("https://a.example/1", "Board"), _final("https://b.example/2", "Web", source="web_search")],
        DAY, reports,
    )
    board, web = views.parse_report((reports / "2026-11-18.md").read_text(encoding="utf-8"))
    assert board["source"] == "your company boards (Lever)"
    assert web["source"] == "web search"
    latest = json.loads((reports / "latest.json").read_text(encoding="utf-8"))
    assert [m["source"] for m in latest["matches"]] == ["lever", "web_search"]


def test_source_labels():
    assert source_label("web_search") == "web search"
    assert source_label("eth_jobs") == "ETH Zurich job board"
    assert source_label("smartrecruiters") == "your company boards (Smartrecruiters)"


# ── practice runs ────────────────────────────────────────────────────────────


def test_a_practice_report_goes_to_its_own_file(tmp_path):
    reports = tmp_path / "reports"
    md, summary = write_report([_final("https://example.com/jobs/1", "Sample")], DAY, reports, practice=True)
    assert md == reports / PRACTICE_REPORT and summary is None
    assert not (reports / "2026-11-18.md").exists()
    assert not (reports / "latest.json").exists()  # the daily email must not see it
    assert load_findings(reports) == {}  # nor the apply step


def test_practice_runs_dont_count_as_the_days_run(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "runs.jsonl").write_text(
        json.dumps({"date": "2026-11-17"}) + "\n"
        + json.dumps({"date": "2026-11-18", "practice": True}) + "\n"
        # Written before the flag existed: known by its only source.
        + json.dumps({"date": "2026-11-19", "sources": [{"name": "fixture"}]}) + "\n",
        encoding="utf-8",
    )
    # Ledgers written before practice runs moved to practice.json still hold them.
    assert last_run_date(reports) == date(2026, 11, 17)
    assert is_practice_run({"sources": [{"name": "fixture"}, {"name": "lever"}]}) is False


def test_the_ui_shows_the_practice_report_only_while_it_is_the_latest_run(tmp_path):
    reports = tmp_path / "reports"
    write_report([_final("https://a.example/1", "Real")], DAY, reports)
    write_report([_final("https://example.com/jobs/1", "Sample", source="fixture")], DAY, reports, practice=True)
    runs = reports / "runs.jsonl"
    runs.write_text(json.dumps({"date": "2026-11-18", "run_at": "2026-11-18T08:00:00+00:00"}) + "\n", encoding="utf-8")
    write_practice_record({"date": "2026-11-18", "run_at": "2026-11-18T09:00:00+00:00", "funnel": {"new": 3}}, reports)

    shown = views.report(tmp_path)
    assert shown["last_run"]["practice"] is True and shown["last_run"]["new"] == 3
    assert [m["title"] for m in shown["practice"]["matches"]] == ["Sample"]
    assert [m["title"] for m in shown["reports"][0]["matches"]] == ["Real"]  # untouched

    # A real run after it retires the practice report.
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": "2026-11-18", "run_at": "2026-11-18T10:00:00+00:00"}) + "\n")
    shown = views.report(tmp_path)
    assert shown["practice"] is None and shown["last_run"]["practice"] is False


def test_a_practice_record_alone_is_the_last_run(tmp_path):
    write_practice_record({"date": "2026-11-18", "run_at": "2026-11-18T09:00:00+00:00"}, tmp_path / "reports")
    assert views.report(tmp_path)["last_run"]["practice"] is True


@pytest.fixture
def practice_root(tmp_path, monkeypatch):
    """Just enough of an instance for search.main.run(dry_run=True) to reach
    the end without a model call: no posting passes the location prefilter."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "search.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "config" / "constraints.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(search_main, "ROOT", tmp_path)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: object())
    monkeypatch.setattr(search_main, "_check_authentication", lambda client: None)
    monkeypatch.setattr(search_main, "load_profile", lambda path: ([], "", []))
    monkeypatch.setattr(search_main, "build_profile_block", lambda *a: "")
    monkeypatch.setattr(search_main, "could_pass_location", lambda *a: False)
    monkeypatch.setattr(
        search_main, "load_companies", lambda root, market: SimpleNamespace(boards=[], notes=[])
    )
    return tmp_path


def test_a_practice_run_leaves_real_state_alone(practice_root):
    seen = practice_root / "data" / "seen_postings.json"
    search_main.run(dry_run=True)
    search_main.run(dry_run=True)  # repeatable: the samples are new every time

    reports = practice_root / "reports"
    assert (reports / PRACTICE_REPORT).exists()
    assert not list(reports.glob("????-??-??.md"))
    assert not (reports / "latest.json").exists()
    assert not seen.exists()  # SeenStore writes only when something is marked

    # The tracked run log is untouched; the record is in the ignored practice.json.
    assert not (reports / "runs.jsonl").exists()
    record = json.loads((reports / "practice.json").read_text(encoding="utf-8"))
    assert record["practice"] is True
    assert record["funnel"]["new"] == 3  # the second run saw the samples as new too
    assert last_run_date(reports) is None


def test_a_practice_run_shows_samples_below_your_floors_and_says_so(practice_root, monkeypatch):
    """The owner's instance has min_interest 40; the sample PM posting scored
    interest 35 and the practice run showed nothing at all."""
    (practice_root / "config" / "search.yaml").write_text(
        "thresholds:\n  min_skill: 60\n  min_interest: 40\n", encoding="utf-8"
    )
    monkeypatch.setattr(search_main, "could_pass_location", lambda *a: True)
    monkeypatch.setattr(search_main, "fill_gaps", lambda p, client, **k: p)
    monkeypatch.setattr(search_main, "filter_postings", lambda postings, c: (postings[:1], []))
    monkeypatch.setattr(
        search_main, "score_postings",
        lambda kept, profile, client: [
            ScoredPosting(posting=kept[0], skill_score=100, interest_score=35, best_cv="cv", brief_reason="")
        ],
    )
    monkeypatch.setattr(
        search_main, "write_rationales",
        lambda ranked, profile, client: [FinalPosting(scored=r.scored, tier=r.tier, writeup="Why.") for r in ranked],
    )
    search_main.run(dry_run=True)

    (sample,) = views.parse_report((practice_root / "reports" / PRACTICE_REPORT).read_text(encoding="utf-8"))
    assert (sample["skill_score"], sample["interest_score"]) == (100, 35)
    assert sample["below_floors"] == (
        "interest 35 is under your floor of 40, so a real search would not show it."
    )
    assert sample["writeup"] == "Why."
    assert not (practice_root / "data" / "seen_postings.json").exists()


def test_a_real_report_never_carries_the_floor_note(tmp_path):
    write_report([_final("https://a.example/1", "Real")], DAY, tmp_path)
    assert "Below your floors" not in (tmp_path / "2026-11-18.md").read_text(encoding="utf-8")


def test_a_warning_on_three_real_runs_in_a_row_is_flagged_in_the_report_and_email(
    practice_root, monkeypatch
):
    board_log = logging.getLogger("jobradar.search.sources.company_pages")

    def fetch_all(sources):
        board_log.warning("Skipping %s (%s): %s", "Optiml", "join", "404 Not Found")
        return [FetchResult("company_pages", [])]

    monkeypatch.setattr(search_main, "_build_sources", lambda *a, **k: [])
    monkeypatch.setattr(search_main, "_fetch_all", fetch_all)
    for _ in range(3):
        search_main.run()

    reports = practice_root / "reports"
    records = [json.loads(line) for line in (reports / "runs.jsonl").read_text().splitlines()]
    assert [r["health"]["warnings"][0]["subjects"] for r in records] == [{"Optiml": 1}] * 3
    summary = json.loads((reports / "latest.json").read_text())
    [flag] = summary["health"]
    assert (flag["kind"], flag["runs"], flag["subjects"], flag["email"]) == (
        "persistent", 3, ["Optiml"], True
    )
    [report] = reports.glob("20*.md")
    assert report.read_text().count("## Run health") == 1  # the third run only

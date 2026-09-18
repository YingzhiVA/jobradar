from datetime import date

from jobradar.search.company_health import (
    StaleFinding,
    first_seen_dates,
    last_active_dates,
    latest_counts,
    load_runs,
    stale_companies,
)


def _run(day: str, counts: dict[str, int]) -> dict:
    return {
        "date": day,
        "sources": [
            {"name": "web_search", "meta": {"queries": ["x"]}},
            {"name": "company_pages", "meta": {"company_counts": counts}},
        ],
    }


def test_last_active_dates_tracks_most_recent_nonzero():
    runs = [
        _run("2026-06-01", {"Acme": 3, "Beta": 0}),
        _run("2026-06-15", {"Acme": 0, "Beta": 2}),
        _run("2026-07-01", {"Acme": 0, "Beta": 0}),
    ]
    assert last_active_dates(runs) == {"Acme": date(2026, 6, 1), "Beta": date(2026, 6, 15)}


def test_first_seen_dates_uses_earliest_appearance_regardless_of_count():
    runs = [
        _run("2026-06-01", {"Acme": 0}),  # present but dry from day one
        _run("2026-06-15", {"Acme": 0, "Beta": 5}),
    ]
    assert first_seen_dates(runs) == {"Acme": date(2026, 6, 1), "Beta": date(2026, 6, 15)}


def test_latest_counts_takes_the_most_recent_run():
    runs = [_run("2026-06-01", {"Acme": 3}), _run("2026-07-01", {"Acme": 0})]
    assert latest_counts(runs) == {"Acme": 0}


def test_stale_companies_flags_only_sustained_dry_boards():
    runs = [
        _run("2026-05-01", {"Dry": 4, "Fresh": 4}),
        _run("2026-06-01", {"Dry": 0, "Fresh": 4}),  # Fresh active recently
        _run("2026-06-25", {"Dry": 0, "Fresh": 9}),
    ]
    findings = stale_companies(runs, ["Dry", "Fresh"], today=date(2026, 7, 1), stale_days=45)

    assert findings == [
        StaleFinding(name="Dry", dry_days=61, last_active=date(2026, 5, 1), last_count=0)
    ]  # Fresh (dry 6 days) is not flagged


def test_stale_companies_new_company_gets_a_grace_window():
    # A company added recently and never yet seen with an ad must NOT be flagged
    # until it's been dry for the full window — its clock starts at first-seen.
    runs = [
        _run("2026-06-20", {"NewCo": 0}),
        _run("2026-06-27", {"NewCo": 0}),
    ]
    assert stale_companies(runs, ["NewCo"], today=date(2026, 7, 1), stale_days=45) == []
    # ... but once the window elapses with no ad ever, it is flagged.
    late = stale_companies(runs, ["NewCo"], today=date(2026, 8, 20), stale_days=45)
    assert [f.name for f in late] == ["NewCo"]
    assert late[0].last_active is None  # never had an ad


def test_stale_companies_ignores_companies_absent_from_history():
    # A company with no runs recorded yet (just added, or feature just shipped)
    # has no data to judge -> not flagged.
    runs = [_run("2026-07-01", {"Acme": 0})]
    assert stale_companies(runs, ["Unknown"], today=date(2026, 8, 30), stale_days=45) == []


def test_stale_companies_sorted_most_stale_first():
    runs = [
        _run("2026-04-01", {"VeryDry": 1}),
        _run("2026-05-15", {"SomewhatDry": 1}),
    ]
    findings = stale_companies(
        runs, ["SomewhatDry", "VeryDry"], today=date(2026, 7, 1), stale_days=30
    )
    assert [f.name for f in findings] == ["VeryDry", "SomewhatDry"]


def test_counts_ignore_runs_from_before_the_feature():
    # Old runs lack company_counts meta; they must be skipped, not crash.
    runs = [
        {"date": "2026-05-01", "sources": [{"name": "company_pages", "meta": {}}]},
        _run("2026-06-01", {"Acme": 2}),
    ]
    assert last_active_dates(runs) == {"Acme": date(2026, 6, 1)}


def test_load_runs_tolerates_missing_file_and_bad_lines(tmp_path):
    assert load_runs(tmp_path / "nope.jsonl") == []
    p = tmp_path / "runs.jsonl"
    p.write_text('{"date": "2026-07-01"}\nnot json\n{"date": "2026-07-02"}\n', encoding="utf-8")
    assert [r["date"] for r in load_runs(p)] == ["2026-07-01", "2026-07-02"]

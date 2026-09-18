from jobradar.discovery.notify_discovery import build_message, load_suggestions


def test_load_suggestions_reads_companies(tmp_path):
    path = tmp_path / "d.yaml"
    path.write_text("companies:\n  - name: Acme\n    ats: lever\n    slug: acme\n")
    assert load_suggestions(path) == [{"name": "Acme", "ats": "lever", "slug": "acme"}]


def test_load_suggestions_missing_file(tmp_path):
    assert load_suggestions(tmp_path / "nope.yaml") == []


def test_load_suggestions_empty_or_no_companies(tmp_path):
    path = tmp_path / "d.yaml"
    path.write_text("# No new companies found this run.\n")
    assert load_suggestions(path) == []


def test_build_message_lists_companies():
    suggestions = [
        {"name": "Acme", "ats": "workable", "slug": "acme", "company_name": "Acme AG"},
        {"name": "Beta", "ats": "lever", "slug": "beta"},
    ]
    subject, body = build_message(suggestions)
    assert "2 new companies" in subject
    assert "Acme (workable / acme)  [Acme AG]" in body
    assert "Beta (lever / beta)" in body


def test_build_message_singular():
    subject, _ = build_message([{"name": "Solo", "ats": "ashby", "slug": "solo"}])
    assert "1 new company" in subject


def test_load_health_report_returns_body_when_flagged(tmp_path):
    from jobradar.discovery.notify_discovery import load_health_report
    p = tmp_path / "company_health.md"
    p.write_text(
        "# Company board health — 2026-07-01\n\n"
        "1 configured board(s)...\n\n## Board gone — act\n\n- **Foo** (lever/foo) — dry 90 days.\n"
    )
    assert "Board gone" in load_health_report(p)


def test_load_health_report_empty_when_clean_or_missing(tmp_path):
    from jobradar.discovery.notify_discovery import load_health_report
    assert load_health_report(tmp_path / "nope.md") == ""
    clean = tmp_path / "company_health.md"
    clean.write_text("# Company board health — 2026-07-01\n\nNo configured board has been dry.\n")
    assert load_health_report(clean) == ""


def test_build_message_includes_health_section_and_subject():
    report = (
        "# Company board health — 2026-07-01\n\n1 board...\n\n"
        "## Board gone — act\n\n- **Foo** (lever/foo) — dry 90 days.\n"
    )
    subject, body = build_message([], health_report=report)
    assert "1 board to check" in subject
    assert "Board gone" in body
    assert "Foo" in body


def test_build_message_combines_companies_and_health():
    report = "## Empty\n\n- **Foo** (lever/foo) — dry 60 days.\n- **Bar** (ashby/bar) — dry 70 days.\n"
    subject, body = build_message(
        [{"name": "New", "ats": "lever", "slug": "new"}], health_report=report
    )
    assert "1 new company" in subject and "2 boards to check" in subject
    assert "New (lever / new)" in body and "Foo" in body

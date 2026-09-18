import os
from pathlib import Path

from jobradar.apply.lint import (
    PDF_STALENESS_CODES,
    LintFinding,
    Severity,
    blocking_errors,
    drop_codes,
    has_errors,
    lint_folder,
    lint_repo,
)
from jobradar.apply.tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    Application,
    Tracker,
)


def _app(**overrides) -> Application:
    base = dict(
        url="https://jobs.example.com/pm-1",
        folder="2026-07-06_acme_pm",
        title="Product Manager",
        company="Acme",
        prepared_on="2026-07-06",
        tier="quick",
        status=STATUS_DRAFTED,
    )
    base.update(overrides)
    return Application(**base)


def _touch(path: Path, content: str = "x", mtime: float | None = None) -> Path:
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _fresh_pdf(md: Path) -> Path:
    """A PDF at least as new as its markdown (the not-stale case)."""
    pdf = md.with_suffix(".pdf")
    _touch(pdf, "%PDF")
    m = md.stat().st_mtime
    os.utime(pdf, (m + 1, m + 1))
    return pdf


# Group-C-clean content: a CV with a contact email, a cover letter that names
# the company, and no em-dash / placeholder / filler.
_CV_BODY = "# Jane Example\n\ncandidate@example.com | LinkedIn\n\n## Profile\n\nProduct and AI expert.\n"
_COVER_BODY = "Dear Acme team,\n\nI would like to work at Acme.\n\nRegards\nJane Example\n"


def _draft(
    root: Path,
    name: str = "2026-07-06_acme_pm",
    cv: str = _CV_BODY,
    cover: str | None = None,
    translation: str | None = None,
) -> Path:
    """A drafted folder with a current PDF per deliverable and controllable
    document contents, for exercising the Group C content checks."""
    folder = root / name
    folder.mkdir()
    _fresh_pdf(_touch(folder / "tailored_cv.md", cv))
    _touch(folder / "notes.md")
    if cover is not None:
        _fresh_pdf(_touch(folder / "cover_letter.md", cover))
    if translation is not None:
        _fresh_pdf(_touch(folder / f"tailored_cv_{translation}.md", cv))
    return folder


def _clean_folder(
    root: Path,
    name: str = "2026-07-06_acme_pm",
    cover_letter: bool = False,
    translation: str | None = None,
) -> Path:
    """A drafted folder that is clean under every check group."""
    return _draft(
        root,
        name,
        cover=_COVER_BODY if cover_letter else None,
        translation=translation,
    )


# --- clean baseline ---------------------------------------------------------


def test_clean_drafted_folder_has_no_findings(tmp_path):
    folder = _clean_folder(tmp_path)
    assert lint_folder(folder, _app()) == []


def test_clean_folder_with_cover_and_translation(tmp_path):
    folder = _clean_folder(tmp_path, cover_letter=True, translation="de")
    assert lint_folder(folder, _app(cover_letter=True, cv_translation_language="de")) == []


# --- Group A: required deliverables -----------------------------------------


def test_missing_tailored_cv_is_error(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    _touch(folder / "notes.md")
    findings = lint_folder(folder, _app(folder="f"))
    assert any(f.code == "missing-deliverable" for f in findings)
    assert has_errors(findings)


def test_cover_letter_required_but_missing_is_error(tmp_path):
    folder = _clean_folder(tmp_path)  # has no cover_letter.md
    findings = lint_folder(folder, _app(cover_letter=True))
    assert any(f.code == "missing-deliverable" and "cover_letter" in f.message for f in findings)


def test_cover_letter_not_required_no_error(tmp_path):
    folder = _clean_folder(tmp_path)
    findings = lint_folder(folder, _app(cover_letter=False))
    assert not any(f.code == "missing-deliverable" for f in findings)


def test_non_drafted_status_skips_required_files(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()  # empty: no deliverables at all
    findings = lint_folder(folder, _app(folder="f", status=STATUS_NEEDS_JD))
    assert not any(f.code == "missing-deliverable" for f in findings)


# --- Group A: translation coherence -----------------------------------------


def test_translation_flag_but_missing_file_is_error(tmp_path):
    folder = _clean_folder(tmp_path)  # no tailored_cv_de.md
    findings = lint_folder(folder, _app(cv_translation_language="de"))
    assert any(f.code == "translation-mismatch" and f.severity is Severity.ERROR for f in findings)


def test_stray_translation_without_flag_warns(tmp_path):
    folder = _clean_folder(tmp_path, translation="de")
    findings = lint_folder(folder, _app(cv_translation_language=None))
    assert any(f.code == "translation-mismatch" and f.severity is Severity.WARN for f in findings)


# --- Group A: status / timestamp coherence ----------------------------------


def test_submitted_without_date_is_error(tmp_path):
    folder = _clean_folder(tmp_path)
    findings = lint_folder(folder, _app(status=STATUS_SUBMITTED, submitted_on=None))
    assert any(f.code == "status-timestamp" and f.severity is Severity.ERROR for f in findings)


def test_drafted_with_submitted_date_warns(tmp_path):
    folder = _clean_folder(tmp_path)
    findings = lint_folder(folder, _app(status=STATUS_DRAFTED, submitted_on="2026-07-07"))
    assert any(f.code == "status-timestamp" and f.severity is Severity.WARN for f in findings)


def test_needs_jd_with_tailored_cv_warns(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    _touch(folder / "tailored_cv.md")
    findings = lint_folder(folder, _app(folder="f", status=STATUS_NEEDS_JD))
    assert any(f.code == "status-timestamp" and f.severity is Severity.WARN for f in findings)


def test_rejected_with_submitted_date_is_coherent(tmp_path):
    # A terminal outcome keeps its submitted_on (still the RAV proof date).
    folder = _clean_folder(tmp_path)
    findings = lint_folder(
        folder,
        _app(status=STATUS_REJECTED, submitted_on="2026-07-07", closed_on="2026-07-20"),
    )
    assert not any(f.code == "status-timestamp" for f in findings)


def test_terminal_status_without_closed_on_warns(tmp_path):
    folder = _clean_folder(tmp_path)
    findings = lint_folder(folder, _app(status=STATUS_REJECTED, submitted_on="2026-07-07"))
    assert any(f.code == "status-timestamp" and f.severity is Severity.WARN for f in findings)


def test_closed_on_with_non_terminal_status_warns(tmp_path):
    folder = _clean_folder(tmp_path)
    findings = lint_folder(folder, _app(status=STATUS_DRAFTED, closed_on="2026-07-20"))
    assert any(f.code == "status-timestamp" and f.severity is Severity.WARN for f in findings)


# --- Group B: PDF staleness -------------------------------------------------


def test_quick_missing_pdf_is_error(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    _touch(folder / "tailored_cv.md")
    _touch(folder / "notes.md")
    findings = lint_folder(folder, _app(folder="f", tier="quick"))
    assert any(f.code == "missing-pdf" and f.severity is Severity.ERROR for f in findings)


def test_full_missing_pdf_is_warn(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    _touch(folder / "tailored_cv.md")
    _touch(folder / "notes.md")
    findings = lint_folder(folder, _app(folder="f", tier="full"))
    pdf_findings = [f for f in findings if f.code == "missing-pdf"]
    assert pdf_findings and all(f.severity is Severity.WARN for f in pdf_findings)


def test_stale_pdf_is_error(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    md = _touch(folder / "tailored_cv.md")
    _touch(folder / "notes.md")
    pdf = _touch(folder / "tailored_cv.pdf")
    base = pdf.stat().st_mtime
    os.utime(pdf, (base, base))
    os.utime(md, (base + 100, base + 100))  # markdown edited long after the PDF
    findings = lint_folder(folder, _app(folder="f"))
    assert any(f.code == "stale-pdf" and f.severity is Severity.ERROR for f in findings)


def test_pdf_within_grace_not_stale(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    md = _touch(folder / "tailored_cv.md")
    _touch(folder / "notes.md")
    pdf = _touch(folder / "tailored_cv.pdf")
    base = pdf.stat().st_mtime
    os.utime(pdf, (base, base))
    os.utime(md, (base + 1, base + 1))  # sub-grace: a checkout race, not an edit
    findings = lint_folder(folder, _app(folder="f"))
    assert not any(f.code == "stale-pdf" for f in findings)


def test_html_leftover_is_error_and_supersedes_missing_pdf(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    _touch(folder / "tailored_cv.md")
    _touch(folder / "notes.md")
    _touch(folder / "tailored_cv.html")
    findings = lint_folder(folder, _app(folder="f"))
    assert any(f.code == "html-leftover" and f.severity is Severity.ERROR for f in findings)
    assert not any(f.code == "missing-pdf" for f in findings)


# --- repo-level integrity ---------------------------------------------------


def test_lint_repo_flags_orphan_folder(tmp_path):
    (tmp_path / "2026-07-06_orphan_pm").mkdir()
    tracker = Tracker(tmp_path / "applications.json")  # empty
    findings = lint_repo(tmp_path, tracker)
    assert any(f.code == "orphan-folder" and f.severity is Severity.WARN for f in findings)


def test_lint_repo_flags_dangling_entry(tmp_path):
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app(folder="missing_folder"))
    findings = lint_repo(tmp_path, tracker)
    assert any(f.code == "dangling-entry" and f.severity is Severity.ERROR for f in findings)


def test_lint_repo_fans_out_and_is_clean(tmp_path):
    _clean_folder(tmp_path)  # tracked, complete
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app())
    assert lint_repo(tmp_path, tracker) == []


def test_lint_repo_skips_the_archive_directory(tmp_path):
    (tmp_path / "archive" / "2026-01-05_old_pm").mkdir(parents=True)
    tracker = Tracker(tmp_path / "applications.json")  # empty
    assert not any(f.code == "orphan-folder" for f in lint_repo(tmp_path, tracker))


# --- Group C: content mechanics ---------------------------------------------


def test_company_missing_from_cover_letter_warns(tmp_path):
    # A WARN, not an ERROR: absence of the tracked name can be a legitimate
    # acronym/short form, so it must not hard-block the --pdf gate.
    folder = _draft(tmp_path, cover="Dear Globex team,\n\nI love Globex.\n\nRegards\n")
    findings = lint_folder(folder, _app(company="Acme", cover_letter=True))
    assert any(f.code == "company-missing" and f.severity is Severity.WARN for f in findings)


def test_company_present_no_error(tmp_path):
    folder = _draft(tmp_path, cover=_COVER_BODY)
    findings = lint_folder(folder, _app(company="Acme", cover_letter=True))
    assert not any(f.code == "company-missing" for f in findings)


def test_company_suffix_stripped_when_matching(tmp_path):
    # Tracker says "Acme AG"; the letter just says "Acme" - still a match.
    folder = _draft(tmp_path, cover=_COVER_BODY)
    findings = lint_folder(folder, _app(company="Acme AG", cover_letter=True))
    assert not any(f.code == "company-missing" for f in findings)


def test_contamination_from_other_application_warns(tmp_path):
    folder = _draft(tmp_path, cover="Dear Acme,\n\nAt Acme, unlike Globex, I ship.\n\nRegards\n")
    findings = lint_folder(
        folder, _app(company="Acme", cover_letter=True), other_companies={"Globex", "Initech"}
    )
    contamination = [f for f in findings if f.code == "company-contamination"]
    assert contamination and all("Globex" in f.message for f in contamination)


def test_missing_contact_email_is_error(tmp_path):
    folder = _draft(tmp_path, cv="# Jane Example\n\n## Profile\n\nNo contact line here.\n")
    findings = lint_folder(folder, _app())
    assert any(f.code == "missing-contact" and f.severity is Severity.ERROR for f in findings)


def test_bracket_placeholder_is_error(tmp_path):
    folder = _draft(tmp_path, cv=_CV_BODY + "\nExperience at [Company Name].\n")
    findings = lint_folder(folder, _app())
    assert any(f.code == "placeholder" and f.severity is Severity.ERROR for f in findings)


def test_markdown_link_not_flagged_as_placeholder(tmp_path):
    folder = _draft(tmp_path, cv="# Y\n\ny@x.com | [LinkedIn](http://x) | [Portfolio](https://y)\n")
    findings = lint_folder(folder, _app())
    assert not any(f.code == "placeholder" for f in findings)


def test_todo_marker_is_error(tmp_path):
    folder = _draft(tmp_path, cv=_CV_BODY + "\nTODO: add metrics\n")
    findings = lint_folder(folder, _app())
    assert any(f.code == "placeholder" and "TODO" in f.message for f in findings)


def test_hiring_manager_placeholder_warns_not_errors(tmp_path):
    folder = _draft(tmp_path, cover="Dear [Hiring manager name],\n\nAcme is great.\n\nRegards\n")
    findings = lint_folder(folder, _app(company="Acme", cover_letter=True))
    assert any(f.code == "unresolved-hiring-manager" and f.severity is Severity.WARN for f in findings)
    assert not any(f.code == "placeholder" for f in findings)  # not double-flagged as an error


def test_em_dash_warns(tmp_path):
    folder = _draft(tmp_path, cv=_CV_BODY + "\nBeispiel Software GmbH — Product Owner\n")
    findings = lint_folder(folder, _app())
    assert any(f.code == "em-dash" and f.severity is Severity.WARN for f in findings)


def test_filler_phrase_warns(tmp_path):
    folder = _draft(tmp_path, cv=_CV_BODY + "\nA proven track record of delivery.\n")
    findings = lint_folder(folder, _app())
    assert any(f.code == "filler" and f.severity is Severity.WARN for f in findings)


def test_long_cover_letter_warns(tmp_path):
    folder = _draft(tmp_path, cover="Dear Acme, " + ("word " * 450) + " Regards")
    findings = lint_folder(folder, _app(company="Acme", cover_letter=True))
    assert any(f.code == "cover-letter-length" and f.severity is Severity.WARN for f in findings)


# --- strict-only checklist gate ---------------------------------------------


def test_strict_checklist_incomplete_is_error_only_in_strict(tmp_path):
    folder = _draft(tmp_path)
    (folder / "notes.md").write_text(
        "## Checklist\n\n- [ ] Review CV\n- [x] Done thing\n", encoding="utf-8"
    )
    strict = lint_folder(folder, _app(), strict=True)
    lenient = lint_folder(folder, _app(), strict=False)
    assert any(f.code == "checklist-incomplete" and f.severity is Severity.ERROR for f in strict)
    assert not any(f.code == "checklist-incomplete" for f in lenient)


def test_strict_checklist_complete_passes(tmp_path):
    folder = _draft(tmp_path)
    (folder / "notes.md").write_text(
        "## Checklist\n\n- [x] Review CV\n- [x] Submit\n", encoding="utf-8"
    )
    findings = lint_folder(folder, _app(), strict=True)
    assert not any(f.code == "checklist-incomplete" for f in findings)


# --- gate helpers -----------------------------------------------------------


def test_blocking_errors_excludes_warnings():
    findings = [
        LintFinding(Severity.ERROR, "placeholder", "m", "f"),
        LintFinding(Severity.WARN, "em-dash", "m", "f"),
    ]
    assert {f.code for f in blocking_errors(findings)} == {"placeholder"}


def test_drop_codes_removes_pdf_staleness_findings():
    findings = [
        LintFinding(Severity.ERROR, "placeholder", "m", "f"),
        LintFinding(Severity.ERROR, "missing-pdf", "m", "f"),
        LintFinding(Severity.WARN, "stale-pdf", "m", "f"),
    ]
    kept = drop_codes(findings, PDF_STALENESS_CODES)
    assert {f.code for f in kept} == {"placeholder"}


# --- helper -----------------------------------------------------------------


def test_has_errors():
    assert not has_errors([])
    assert has_errors([LintFinding(Severity.ERROR, "x", "m", "f")])
    assert not has_errors([LintFinding(Severity.WARN, "x", "m", "f")])


_ENGLISH_CV = """# Jane Doe

## Experience

- Owned the analytics roadmap for the payments team and shipped the first
  version of the reporting stack, from the data model to the dashboards.
- Worked with engineering on the migration and ran the rollout in three
  markets, with a weekly review of the adoption numbers.
- Built the risk model that flags accounts for review, and monitored it in
  production with a dashboard the support team reads every morning.
"""

_GERMAN_CV = """# Jane Doe

## Erfahrung

- Verantwortete die Analytics-Roadmap für das Payments-Team und lieferte die
  erste Version des Reportings, von dem Datenmodell bis zu den Dashboards.
- Arbeitete mit dem Engineering an der Migration und führte den Rollout in
  drei Märkten durch, mit einem wöchentlichen Review von den Zahlen.
- Baute das Risikomodell, das die Konten für die Prüfung markiert, und
  überwachte es in der Produktion mit einem Dashboard von dem Support-Team.
"""


def test_cv_language_flags_a_german_cv_under_the_english_name(tmp_path):
    folder = tmp_path / "2026-07-06_acme_pm"
    folder.mkdir()
    _touch(folder / "tailored_cv.md", _GERMAN_CV)
    findings = lint_folder(folder, None)
    flagged = [f for f in findings if f.code == "cv-language"]
    assert len(flagged) == 1
    assert flagged[0].severity is Severity.WARN
    assert "reads as German" in flagged[0].message


def test_cv_language_accepts_each_cv_in_its_own_language(tmp_path):
    folder = tmp_path / "2026-07-06_acme_pm"
    folder.mkdir()
    _touch(folder / "tailored_cv.md", _ENGLISH_CV)
    _touch(folder / "tailored_cv_de.md", _GERMAN_CV)
    assert [f for f in lint_folder(folder, None) if f.code == "cv-language"] == []


def test_cv_language_flags_a_translation_that_stayed_english(tmp_path):
    folder = tmp_path / "2026-07-06_acme_pm"
    folder.mkdir()
    _touch(folder / "tailored_cv.md", _ENGLISH_CV)
    _touch(folder / "tailored_cv_de.md", _ENGLISH_CV)
    flagged = [f for f in lint_folder(folder, None) if f.code == "cv-language"]
    assert len(flagged) == 1
    assert "tailored_cv_de.md reads as English" in flagged[0].message


def test_cv_language_stays_quiet_on_a_document_too_short_to_call(tmp_path):
    folder = tmp_path / "2026-07-06_acme_pm"
    folder.mkdir()
    _touch(folder / "tailored_cv.md", "# Jane Doe\n\n## Experience\n\n- Analytics, SQL, Python\n")
    assert [f for f in lint_folder(folder, None) if f.code == "cv-language"] == []


def test_cv_language_skips_a_language_it_has_no_markers_for(tmp_path):
    folder = tmp_path / "2026-07-06_acme_pm"
    folder.mkdir()
    _touch(folder / "tailored_cv.md", _ENGLISH_CV)
    _touch(folder / "tailored_cv_nl.md", _GERMAN_CV)
    assert [f for f in lint_folder(folder, None) if f.code == "cv-language"] == []


# --- Group C: the relevance cut ---------------------------------------------

_BASE_CV = (
    "# Jane Example\n\ncandidate@example.com | LinkedIn\n\n"
    "**Beispiel Software GmbH** *- Product Owner*\n\n"
    "* Built a GenAI advisory proof of concept.\n"
    "* Restructured meetings around clear objectives.\n"
    "* Shipped a risk-scoring model.\n"
    "* Ran the quarterly planning cadence.\n"
)


def _cvs_dir(root: Path, label: str = "CV_X", body: str = _BASE_CV) -> Path:
    cvs = root / "cvs"
    cvs.mkdir(exist_ok=True)
    _touch(cvs / f"{label}.md", body)
    return cvs


def _cut(body: str, *drop: str) -> str:
    """The base CV minus the bullets whose text contains any of `drop` - what a
    tailoring call that actually applied its relevance filter returns."""
    return "\n".join(
        line for line in body.splitlines() if not any(d in line for d in drop)
    )


def test_no_bullets_cut_flags_a_reordered_but_untrimmed_cv(tmp_path):
    # Every bullet survived; only their order and wording changed.
    reordered = (
        "# Jane Example\n\ncandidate@example.com | LinkedIn\n\n"
        "**Beispiel Software GmbH** *- Product Owner*\n\n"
        "* Shipped a risk-scoring model.\n"
        "* Built a GenAI advisory PoC end to end.\n"
        "* Ran the quarterly planning cadence.\n"
        "* Restructured meetings around clear objectives.\n"
    )
    folder = _draft(tmp_path, cv=reordered)
    findings = lint_folder(folder, _app(base_cv="CV_X"), cvs_dir=_cvs_dir(tmp_path))
    assert [f.code for f in findings] == ["no-bullets-cut"]
    assert findings[0].severity is Severity.WARN


def test_no_bullets_cut_silent_once_a_bullet_is_dropped(tmp_path):
    folder = _draft(
        tmp_path, cv=_cut(_BASE_CV, "Restructured meetings", "planning cadence")
    )
    findings = lint_folder(folder, _app(base_cv="CV_X"), cvs_dir=_cvs_dir(tmp_path))
    assert [f for f in findings if f.code == "no-bullets-cut"] == []


def test_no_bullets_cut_needs_the_cvs_dir(tmp_path):
    # Without profile/cvs there is nothing to compare against, so the check
    # stays quiet rather than guessing.
    folder = _draft(tmp_path, cv=_BASE_CV)
    assert [f for f in lint_folder(folder, _app(base_cv="CV_X")) if f.code == "no-bullets-cut"] == []


def test_no_bullets_cut_skips_a_base_cv_that_is_gone(tmp_path):
    folder = _draft(tmp_path, cv=_BASE_CV)
    findings = lint_folder(folder, _app(base_cv="CV_RENAMED"), cvs_dir=_cvs_dir(tmp_path))
    assert [f for f in findings if f.code == "no-bullets-cut"] == []


def test_no_bullets_cut_does_not_count_bold_labels_or_rules_as_bullets(tmp_path):
    # "**Skills:**" starts with a star and "---" with a dash, but neither is a
    # list item; a CV built only from those has nothing to cut and must not warn.
    bulletless = (
        "# Jane Example\n\ncandidate@example.com | LinkedIn\n\n---\n\n"
        "**Skills:** Python, SQL\n\n**Languages:** German, English\n"
    )
    folder = _draft(tmp_path, cv=bulletless)
    findings = lint_folder(
        folder, _app(base_cv="CV_X"), cvs_dir=_cvs_dir(tmp_path, body=bulletless)
    )
    assert [f for f in findings if f.code == "no-bullets-cut"] == []

from pathlib import Path

import pytest

from jobradar import doctor
from jobradar.doctor import FAIL, OK, WARN, TEMPLATE_MARKER, run_checks

_ROOT = Path(__file__).resolve().parents[1]


def _setup(tmp_path, *, cv="# Jane Example\n\nProduct manager.", identity="## Who I am\n\nReal text."):
    (tmp_path / "profile" / "cvs").mkdir(parents=True)
    if cv is not None:
        (tmp_path / "profile" / "cvs" / "mine.md").write_text(cv, encoding="utf-8")
    if identity is not None:
        (tmp_path / "profile" / "identity.md").write_text(identity, encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "companies.yaml").write_text(
        "companies:\n  - name: Acme\n    ats: greenhouse\n    slug: acme\n", encoding="utf-8"
    )
    (tmp_path / "config" / "constraints.yaml").write_text(
        (_ROOT / "config" / "constraints.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def _by_name(checks):
    return {c.name: c for c in checks}


def test_all_good(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    checks = _by_name(run_checks(_setup(tmp_path), connect=lambda: None))
    assert checks["Anthropic credentials"].level == OK
    assert checks["CVs"].level == OK
    assert checks["Identity"].level == OK
    assert checks["companies.yaml"].level == OK
    assert checks["constraints.yaml"].level == OK
    assert checks["search.yaml"].level == OK
    assert checks["retention.yaml"].level == OK
    assert "Git merge driver" not in checks  # no .gitattributes in tmp_path


def test_placeholder_key_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "your_anthropic_api_key_here")
    checks = _by_name(run_checks(_setup(tmp_path), connect=lambda: None))
    assert checks["Anthropic credentials"].level == FAIL
    assert "placeholder" in checks["Anthropic credentials"].message


def test_failed_preflight_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bad")

    def boom():
        raise RuntimeError("Anthropic API authentication failed. Locally, check ANTHROPIC_API_KEY")

    checks = _by_name(run_checks(_setup(tmp_path), connect=boom))
    assert checks["Anthropic credentials"].level == FAIL
    assert "authentication failed" in checks["Anthropic credentials"].message


def test_missing_cv_fails_and_template_profile_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    root = _setup(tmp_path, cv=None, identity=f"<!-- {TEMPLATE_MARKER} replace me -->\n# Identity")
    checks = _by_name(run_checks(root, connect=lambda: None))
    assert checks["CVs"].level == FAIL
    assert checks["Identity"].level == WARN

    (root / "profile" / "cvs" / "example.md").write_text(f"<!-- {TEMPLATE_MARKER} -->\n# CV", encoding="utf-8")
    checks = _by_name(run_checks(root, connect=lambda: None))
    assert checks["CVs"].level == WARN
    assert "template" in checks["CVs"].message


def test_real_cv_next_to_template_says_to_delete_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    root = _setup(tmp_path)
    (root / "profile" / "cvs" / "example.md").write_text(f"<!-- {TEMPLATE_MARKER} -->\n# CV", encoding="utf-8")
    checks = _by_name(run_checks(root, connect=lambda: None))
    assert checks["CVs"].level == OK
    assert "delete the template example.md" in checks["CVs"].message


def test_bad_yaml_is_a_failure_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    root = _setup(tmp_path)
    (root / "config" / "search.yaml").write_text("thresholds:\n  min_skill: 140\n", encoding="utf-8")
    (root / "config" / "companies.yaml").write_text("companies: [unclosed\n", encoding="utf-8")
    checks = _by_name(run_checks(root, connect=lambda: None))
    assert checks["search.yaml"].level == FAIL
    assert "thresholds.min_skill" in checks["search.yaml"].message
    assert checks["companies.yaml"].level == FAIL


def test_empty_company_list_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    root = _setup(tmp_path)
    (root / "config" / "companies.yaml").write_text("companies: []\n", encoding="utf-8")
    assert _by_name(run_checks(root, connect=lambda: None))["companies.yaml"].level == WARN


def test_main_exits_nonzero_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(doctor, "ROOT", _setup(tmp_path, cv=None))
    monkeypatch.setattr(doctor, "_default_connect", lambda: None)
    with pytest.raises(SystemExit) as excinfo:
        doctor.main([])
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL] CVs" in out
    assert "problem(s) to fix" in out


def test_main_reports_ready(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(doctor, "ROOT", _setup(tmp_path))
    monkeypatch.setattr(doctor, "_default_connect", lambda: None)
    doctor.main([])
    assert "Ready to run" in capsys.readouterr().out


# --- companies.yaml: opt-in list, and the traps of editing it -------------------


def _companies(root, text):
    (root / "config" / "companies.yaml").write_text(text, encoding="utf-8")
    return doctor.check_companies(root)


def test_all_commented_list_warns_and_points_at_the_file(tmp_path):
    root = _setup(tmp_path)
    check = _companies(root, "companies:\n  # - name: Acme\n  #   ats: greenhouse\n  #   slug: acme\n")
    assert check.level == WARN
    assert "no company boards selected" in check.message


def test_half_uncommented_entry_fails_with_its_name(tmp_path):
    root = _setup(tmp_path)
    check = _companies(root, "companies:\n  - name: Acme\n  #   ats: greenhouse\n  #   slug: acme\n")
    assert check.level == FAIL
    assert "Acme: missing ats, slug" in check.message


def test_orphaned_lines_merging_into_the_entry_above_fail(tmp_path):
    # The Swiss Re trap: comment out a `- name:` line but not its ats/slug, and
    # PyYAML silently hands them to the previous entry.
    root = _setup(tmp_path)
    check = _companies(
        root,
        "companies:\n"
        "  - name: Swiss Re\n    ats: successfactors\n    slug: careers.swissre.com\n"
        "  # - name: Jobgether\n    ats: lever\n    slug: jobgether\n",
    )
    assert check.level == FAIL
    assert "appears twice" in check.message


def test_unknown_ats_fails(tmp_path):
    root = _setup(tmp_path)
    check = _companies(root, "companies:\n  - name: Acme\n    ats: myspace\n    slug: acme\n")
    assert check.level == FAIL
    assert "unknown ats" in check.message


def test_many_boards_warns_about_the_first_run(tmp_path):
    root = _setup(tmp_path)
    many = "".join(
        f"  - name: Co {i}\n    ats: greenhouse\n    slug: co{i}\n" for i in range(doctor.MANY_BOARDS + 1)
    )
    check = _companies(root, "companies:\n" + many)
    assert check.level == WARN
    assert "first run" in check.message


def test_a_few_boards_is_fine(tmp_path):
    root = _setup(tmp_path)
    check = _companies(root, "companies:\n  - name: Acme\n    ats: greenhouse\n    slug: acme\n")
    assert check.level == OK
    assert check.message == "1 boards"


def test_the_shipped_company_list_is_well_formed():
    # Holds for the public template (no active boards) and for a private copy
    # with boards switched on alike: whatever is active must be complete, and
    # no entry may have swallowed another's lines.
    import yaml

    text = (_ROOT / "config" / "companies.yaml").read_text(encoding="utf-8")
    data = yaml.load(text, Loader=doctor._NoDuplicateKeysLoader) or {}
    assert doctor.validate_companies(data.get("companies") or []) == []

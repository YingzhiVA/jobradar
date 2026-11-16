import io
import zipfile
from pathlib import Path

import pytest
import yaml

from jobradar.doctor import TEMPLATE_MARKER
from jobradar.models import Constraints
from jobradar.ui import setup
from jobradar.ui.setup import SetupError

CV_TEXT = "# Jane Example\n\n" + "Regulatory affairs manager with ten years in pharma. " * 8
IDENTITY = [
    {"heading": "Who I am professionally", "body": "I lead regulatory submissions. " * 8},
    {"heading": "What I'm moving toward", "body": "A strategy role in a small biotech. " * 4},
    {"heading": "My academic background", "body": ""},
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An instance as the template ships it: example CV, template identity,
    no boards selected."""
    (tmp_path / "profile" / "cvs").mkdir(parents=True)
    (tmp_path / "profile" / "cvs" / "example.md").write_text(
        f"<!-- {TEMPLATE_MARKER} replace -->\n# Example\n", encoding="utf-8"
    )
    (tmp_path / "profile" / "identity.md").write_text(
        f"<!-- {TEMPLATE_MARKER} replace -->\n# Career identity\n\n## Who I am\n\nprompt\n", encoding="utf-8"
    )
    boards = tmp_path / "config" / "boards"
    boards.mkdir(parents=True)
    (boards / "global.yaml").write_text(
        "boards:\n  - name: Globex\n    ats: greenhouse\n    slug: globex\n", encoding="utf-8"
    )
    (boards / "ch.yaml").write_text(
        "boards:\n"
        "  # ── Pharma & biotech ─────────────\n"
        "  - name: Acme Pharma\n    ats: greenhouse\n    slug: acme\n"
        "  # A comment between two entries.\n"
        "  - name: \"Biotix\"  # quoted, with a trailing comment\n    ats: lever\n    slug: biotix\n"
        "  # ── Software ─────────────────────\n"
        "  - name: Codeworks\n    ats: ashby\n    slug: codeworks\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "companies.yaml").write_text("companies:\n", encoding="utf-8")
    (tmp_path / "config" / "constraints.yaml").write_text(
        "locations:\n  allowed_cantons: [\"Zurich\", \"St.Gallen\", \"Basel\"]\n"
        "  allowed_cities: [\"Konstanz\"]\n  remote_countries: [\"Switzerland\", \"Germany\"]\n"
        "employment:\n  min_percentage: 80\n  max_percentage: 100\n"
        "work_authorization:\n  requires_sponsorship: true\n",
        encoding="utf-8",
    )
    return tmp_path


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


# ── status ──────────────────────────────────────────────────────────────────


def test_a_fresh_template_has_every_step_open(root):
    status = setup.status(root)
    assert status["steps"] == dict.fromkeys(setup.STEPS, False)
    assert not status["ready"] and not status["has_run"]


def test_an_instance_that_has_run_from_the_terminal_has_nothing_left_to_set_up(root):
    setup.save_cv_text(root, "mine", CV_TEXT)
    setup.save_identity(root, IDENTITY)
    (root / "data").mkdir()
    (root / "data" / "seen_postings.json").write_text('{"abc": {}}', encoding="utf-8")
    # No key in .env (an `ant auth login` user) and no step confirmed in the
    # UI: a run having happened is proof enough of both.
    assert setup.status(root)["ready"]


def test_the_two_steps_that_ship_with_working_values_count_once_saved(root):
    setup.save_key(root, "sk-ant-" + "x" * 30, lambda: None)
    setup.save_cv_text(root, "mine", CV_TEXT)
    setup.save_identity(root, IDENTITY)
    assert setup.status(root)["steps"]["constraints"] is False
    setup.save_constraints(root, {"allowed_regions": ["Zurich"], "min_percentage": 80, "max_percentage": 100})
    setup.save_boards(root, [])
    assert setup.status(root)["ready"]


# ── key ─────────────────────────────────────────────────────────────────────


def test_key_is_written_once_and_never_returned(root, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (root / ".env.example").write_text(
        "# comment\nANTHROPIC_API_KEY=your_anthropic_api_key_here\n# JOBRADAR_X=1\n", encoding="utf-8"
    )
    assert setup.key_status(root) == {"set": False, "hint": ""}
    first = setup.save_key(root, "  sk-ant-" + "a" * 30 + "\n", lambda: None)
    second = setup.save_key(root, "sk-ant-" + "b" * 26 + "wxyz", lambda: None)
    text = (root / ".env").read_text(encoding="utf-8")
    assert text.count("ANTHROPIC_API_KEY=") == 1 and "# JOBRADAR_X=1" in text
    assert first["works"] and second == {"set": True, "hint": "sk-ant-…wxyz", "works": True, "error": ""}


def test_the_key_file_is_private_to_its_owner(root, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = root / ".env"
    env.write_text("ANTHROPIC_API_KEY=old\n", encoding="utf-8")
    env.chmod(0o644)
    setup.save_key(root, "sk-ant-api03-" + "a" * 30, lambda: None)
    assert env.stat().st_mode & 0o777 == 0o600
    env.unlink()
    setup.save_key(root, "sk-ant-api03-" + "b" * 30, lambda: None)
    assert env.stat().st_mode & 0o777 == 0o600


def test_a_rejected_key_is_reported_not_raised(root, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def refuse():
        raise RuntimeError("Anthropic API authentication failed. Locally, check…")

    result = setup.save_key(root, "sk-ant-" + "a" * 30, refuse)
    assert result["works"] is False and "did not accept" in result["error"]


@pytest.mark.parametrize("key", [
    "", "hello", "sk-ant-short", "sk-ant-" + "a" * 20 + " b",
    "sk-ant-" + "a" * 20 + "${HOME}", "sk-ant-" + "a" * 20 + "#x", 'sk-ant-' + "a" * 20 + '"',
    "sk-ant-" + "a" * 20 + "\nOTHER=1",
])
def test_something_that_is_not_a_key_is_refused_before_it_is_stored(root, key):
    with pytest.raises(SetupError):
        setup.save_key(root, key, lambda: None)
    assert not (root / ".env").exists()


# ── CVs ─────────────────────────────────────────────────────────────────────


def test_first_real_cv_replaces_the_shipped_example(root):
    saved = setup.save_cv_text(root, "Regulatory Affairs!", CV_TEXT)
    assert saved["name"] == "Regulatory_Affairs"
    assert [cv["name"] for cv in setup.list_cvs(root)] == ["Regulatory_Affairs"]


def test_word_upload_becomes_markdown(root):
    saved = setup.save_cv_file(root, "Jane CV (final).docx", _docx(["Led twelve submissions to Swissmedic."] * 3))
    assert "Led twelve submissions to Swissmedic." in saved["text"]
    assert setup.list_cvs(root)[0]["from_word"] == "Jane_CV_final.docx"


def test_unreadable_word_file_leaves_nothing_behind(root):
    with pytest.raises(SetupError):
        setup.save_cv_file(root, "cv.docx", b"this is not a zip archive")
    assert [p.name for p in (root / "profile" / "cvs").iterdir()] == ["example.md"]


@pytest.mark.parametrize("name", ["cv.pdf", "cv.doc", "cv.exe"])
def test_formats_that_cannot_be_read_are_refused_with_what_to_do(root, name):
    with pytest.raises(SetupError):
        setup.save_cv_file(root, name, b"data")


@pytest.mark.parametrize("name", ["../../etc/passwd", "..", "README", "///"])
def test_cv_names_cannot_leave_the_folder_or_shadow_the_readme(root, name):
    try:
        setup.save_cv_text(root, name, CV_TEXT)
    except SetupError:
        pass
    written = {p.resolve().parent for p in root.rglob("*.md")}
    assert written <= {root / "profile" / "cvs", root / "profile"}
    assert not (root / "profile" / "cvs" / "README.md").exists()


def test_delete_removes_the_word_file_too(root):
    setup.save_cv_file(root, "jane.docx", _docx(["Led twelve submissions to Swissmedic."] * 3))
    setup.delete_cv(root, "jane")
    assert list((root / "profile" / "cvs").glob("jane.*")) == []


# ── identity ────────────────────────────────────────────────────────────────


def test_template_identity_reads_as_empty_questions(root):
    data = setup.read_identity(root)
    assert data["template"]
    assert [s["heading"] for s in data["sections"]] == [h for h, _ in setup.IDENTITY_SECTIONS]
    assert all(s["body"] == "" and s["hint"] for s in data["sections"])


def test_identity_round_trips_and_drops_unanswered_questions(root):
    setup.save_identity(root, IDENTITY)
    text = (root / "profile" / "identity.md").read_text(encoding="utf-8")
    assert TEMPLATE_MARKER not in text and "My academic background" not in text
    data = setup.read_identity(root)
    assert not data["template"]
    assert [s["heading"] for s in data["sections"]] == ["Who I am professionally", "What I'm moving toward"]


def test_a_hand_written_identity_keeps_its_own_headings_and_its_note_on_top(root):
    page = (
        "# My page\n\nA note to myself on how to write this.\n\n"
        "## My work eligibility\n\nSwiss C permit.\n\n## What I'm moving toward\n\nOld answer.\n"
    )
    path = root / "profile" / "identity.md"
    path.write_text(page, encoding="utf-8")
    sections = setup.read_identity(root)["sections"]
    # The note above the first section is not a field: it answers nothing.
    assert [(s["heading"], s["body"]) for s in sections] == [
        ("My work eligibility", "Swiss C permit."), ("What I'm moving toward", "Old answer."),
    ]
    sections[1]["body"] = "A strategy role in a small biotech. " * 8
    setup.save_identity(root, sections)
    saved = path.read_text(encoding="utf-8")
    assert saved.startswith("# My page\n\nA note to myself on how to write this.\n\n## My work eligibility\n\nSwiss C permit.")
    assert "Old answer." not in saved


def test_a_page_with_no_sections_is_never_a_field_and_is_kept_above_the_answers(root):
    path = root / "profile" / "identity.md"
    path.write_text("# About me\n\nI lead submissions.\n", encoding="utf-8")
    sections = setup.read_identity(root)["sections"]
    assert [s["heading"] for s in sections] == [h for h, _ in setup.IDENTITY_SECTIONS]
    assert all(s["heading"] and s["body"] == "" for s in sections)
    sections[0]["body"] = "I lead regulatory submissions. " * 10
    setup.save_identity(root, sections + [{"heading": "", "body": "a stray untitled box"}])
    saved = path.read_text(encoding="utf-8")
    assert saved.startswith("# About me\n\nI lead submissions.\n\n## Who I am professionally\n\nI lead regulatory")
    assert "stray" not in saved


def test_a_page_without_a_title_gets_one_and_keeps_its_text(root):
    path = root / "profile" / "identity.md"
    path.write_text("Some notes.\n\n## Who I am professionally\n\nOld.\n", encoding="utf-8")
    setup.save_identity(root, [{"heading": "Who I am professionally", "body": "I lead regulatory submissions. " * 10}])
    assert path.read_text(encoding="utf-8").startswith(f"{setup.IDENTITY_TITLE}\n\nSome notes.\n\n## Who I am")


def test_a_thin_identity_is_refused(root):
    with pytest.raises(SetupError):
        setup.save_identity(root, [{"heading": "Who I am professionally", "body": "A manager."}])


# ── constraints ─────────────────────────────────────────────────────────────


def test_hand_spelled_cantons_tick_their_checkbox(root):
    form = setup.read_constraints(root)
    assert form["allowed_regions"] == ["Zurich", "St. Gallen"]
    assert form["remote_in_country"]


def test_saving_constraints_keeps_what_the_form_does_not_show(root):
    setup.save_constraints(root, {
        "allowed_regions": ["Zug", "Zurich", "Atlantis"], "remote_in_country": False,
        "min_percentage": "60", "max_percentage": "100", "max_office_days": "", "dealbreakers": "Internships\n\n",
    })
    data = yaml.safe_load((root / "config" / "constraints.yaml").read_text(encoding="utf-8"))
    # "Basel" is no canton's name, so it has no checkbox; it was typed by hand.
    assert data["locations"]["allowed_cantons"] == ["Zurich", "Basel", "Zug"]
    assert data["locations"]["allowed_cities"] == ["Konstanz"]
    assert data["locations"]["remote_countries"] == ["Germany"]
    assert data["work_authorization"] == {"requires_sponsorship": True}
    parsed = Constraints.from_dict(data)
    assert (parsed.min_percentage, parsed.max_office_days_per_week) == (60, None)
    assert parsed.other_dealbreakers == ["Internships"]


@pytest.mark.parametrize("form", [
    {"allowed_regions": [], "remote_in_country": False, "min_percentage": 80, "max_percentage": 100},
    {"allowed_regions": ["Zurich"], "min_percentage": 100, "max_percentage": 80},
    {"allowed_regions": ["Zurich"], "min_percentage": "lots", "max_percentage": 100},
    {"allowed_regions": ["Zurich"], "min_percentage": 80, "max_percentage": 100, "max_office_days": 9},
])
def test_constraints_no_posting_could_pass_are_refused_and_nothing_is_written(root, form):
    before = (root / "config" / "constraints.yaml").read_text(encoding="utf-8")
    with pytest.raises(SetupError):
        setup.save_constraints(root, form)
    assert (root / "config" / "constraints.yaml").read_text(encoding="utf-8") == before


# ── boards ──────────────────────────────────────────────────────────────────


def test_boards_are_grouped_under_the_catalogue_headings(root):
    data = setup.read_boards(root)
    assert [(s["name"], [b["name"] for b in s["boards"]]) for s in data["sections"]] == [
        (setup.GLOBAL_SECTION, ["Globex"]),
        ("Pharma & biotech", ["Acme Pharma", "Biotix"]),
        ("Software", ["Codeworks"]),
    ]
    assert data["selected"] == 0 and data["first_run"]


def test_saving_boards_keeps_hand_added_ones(root):
    (root / "config" / "companies.yaml").write_text(
        "companies:\n  - Codeworks\n"
        "  - name: Local Lab\n    ats: greenhouse\n    slug: locallab\n"
        "  - name: Biotix\n    ats: greenhouse\n    slug: biotix-own\n",
        encoding="utf-8",
    )
    assert setup.save_boards(root, ["biotix", "Acme Pharma"]) == 3
    companies = yaml.safe_load((root / "config" / "companies.yaml").read_text(encoding="utf-8"))["companies"]
    assert companies == [
        {"name": "Local Lab", "ats": "greenhouse", "slug": "locallab"},  # not in the catalogue
        {"name": "Biotix", "ats": "greenhouse", "slug": "biotix-own"},  # their own board for it
        "Acme Pharma",
    ]
    data = setup.read_boards(root)
    assert data["own"] == ["Local Lab"] and data["selected"] == 3


def test_a_board_the_catalogue_lacks_is_refused(root):
    with pytest.raises(SetupError, match="Nope"):
        setup.save_boards(root, ["Acme Pharma", "Nope"])


# ── search dials ────────────────────────────────────────────────────────────


def test_search_dials_keep_the_sections_the_form_does_not_show(root):
    (root / "config" / "search.yaml").write_text(
        "schedule:\n  frequency: weekly\nsources:\n  eth_jobs:\n    enabled: true\n", encoding="utf-8"
    )
    form = {**setup.read_search(root), "min_interest": 40, "rav": True}
    setup.save_search(root, form)
    data = yaml.safe_load((root / "config" / "search.yaml").read_text(encoding="utf-8"))
    assert data["schedule"] == {"frequency": "weekly"} and data["sources"]["eth_jobs"]["enabled"]
    assert setup.read_search(root) == form


def test_dials_that_would_surface_nothing_are_refused(root):
    form = {**setup.read_search(root), "max_best": 0, "max_okay": 0}
    with pytest.raises(SetupError):
        setup.save_search(root, form)
    assert not (root / "config" / "search.yaml").exists()


# ── hand-written files keep their text ──────────────────────────────────────

COMMENTED_CONSTRAINTS = """\
# My requirements.
locations:
  # Reachable by train.
  allowed_cantons:
    - "Zurich"              # home
    - "St.Gallen"           # ~60 min
    - "Bern"                # too far, really
  remote_countries:
    - "Switzerland"

employment:
  min_percentage: 80   # never below
  max_percentage: 100

office_days:
  max_per_week: 4
"""


def test_saving_must_haves_changes_only_the_lines_that_changed(root):
    path = root / "config" / "constraints.yaml"
    path.write_text(COMMENTED_CONSTRAINTS, encoding="utf-8")
    form = setup.read_constraints(root)
    form["allowed_regions"] = ["Zurich", "St. Gallen", "Zug"]
    form["min_percentage"] = 60
    setup.save_constraints(root, form)
    assert path.read_text(encoding="utf-8") == COMMENTED_CONSTRAINTS.replace(
        '    - "Bern"                # too far, really\n', "    - Zug\n"
    ).replace("min_percentage: 80   # never below", "min_percentage: 60   # never below")


def test_saving_a_form_unchanged_leaves_the_file_byte_for_byte(root):
    path = root / "config" / "constraints.yaml"
    path.write_text(COMMENTED_CONSTRAINTS + "other_dealbreakers:\n  - Internships\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    setup.save_constraints(root, setup.read_constraints(root))
    assert path.read_text(encoding="utf-8") == before


def test_selecting_boards_keeps_the_section_comments(root):
    path = root / "config" / "companies.yaml"
    path.write_text(
        "# My boards.\ncompanies:\n\n  # ── Pharma ──\n  - Acme Pharma\n  - Biotix   # friend works here\n"
        "\n  # ── Software ──\n  - Codeworks\n",
        encoding="utf-8",
    )
    setup.save_boards(root, ["Biotix", "Codeworks", "Globex"])
    assert path.read_text(encoding="utf-8") == (
        "# My boards.\ncompanies:\n\n  # ── Pharma ──\n  - Biotix   # friend works here\n"
        "\n  # ── Software ──\n  - Codeworks\n  - Globex\n"
    )


def test_search_dials_keep_comments_and_leave_defaults_unmentioned(root):
    path = root / "config" / "search.yaml"
    path.write_text("thresholds:\n  # how picky\n  min_skill: 60   # [60]\n", encoding="utf-8")
    setup.save_search(root, {**setup.read_search(root), "min_skill": 70})
    assert path.read_text(encoding="utf-8") == "thresholds:\n  # how picky\n  min_skill: 70   # [60]\n"


def test_a_file_too_unusual_to_edit_in_place_is_still_saved_correctly(root):
    path = root / "config" / "constraints.yaml"
    path.write_text(
        "locations: {allowed_cantons: [Zurich], remote_countries: []}\nemployment: {min_percentage: 80, max_percentage: 100}\n",
        encoding="utf-8",
    )
    setup.save_constraints(root, {**setup.read_constraints(root), "allowed_regions": ["Zug"], "min_percentage": 50})
    saved = setup.read_constraints(root)
    assert saved["allowed_regions"] == ["Zug"] and saved["min_percentage"] == 50



def test_the_form_keeps_the_per_search_counts_in_range(root):
    for strong, okay in ((0, 3), (6, 3), (1, 11)):
        with pytest.raises(SetupError, match="between"):
            setup.save_search(root, {**setup.read_search(root), "max_best": strong, "max_okay": okay})
    assert not (root / "config" / "search.yaml").exists()


def test_warnings_for_values_still_being_typed(root):
    form = {**setup.read_search(root), "min_skill": "45"}
    assert [w["key"] for w in setup.search_warnings(form)] == ["min_skill"]
    assert setup.search_warnings({**form, "min_skill": ""}) == []  # a field mid-edit

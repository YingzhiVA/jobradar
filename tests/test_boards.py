from pathlib import Path

import pytest

from jobradar.boards import (
    BoardsError,
    catalog_problems,
    index_catalog,
    load_companies,
    read_catalog,
    resolve,
)

_ROOT = Path(__file__).resolve().parents[1]

_CATALOG = [
    {"name": "Frontify", "ats": "ashby", "slug": "frontify"},
    {"name": "Huawei (Zurich)", "ats": "teamtailor", "slug": "huaweizurich"},
]


def _resolve(selection, catalog=_CATALOG):
    return resolve(selection, index_catalog(catalog))


# --- resolving the selection ---------------------------------------------------


def test_a_name_takes_the_catalogue_board():
    result = _resolve(["Frontify"])
    assert result.boards == [{"name": "Frontify", "ats": "ashby", "slug": "frontify", "origin": "catalog"}]
    assert result.notes == []


def test_names_match_regardless_of_case_and_spacing_and_report_the_catalogue_spelling():
    result = _resolve(["  frontify ", "huawei  (zurich)"])
    assert [b["name"] for b in result.boards] == ["Frontify", "Huawei (Zurich)"]
    assert result.notes == []


def test_a_name_block_without_ats_or_slug_counts_as_a_plain_name():
    result = _resolve([{"name": "Frontify"}])
    assert result.boards[0]["origin"] == "catalog"


def test_an_unknown_name_is_an_error_and_is_not_scanned():
    result = _resolve(["Frontfy"])
    assert result.boards == []
    assert result.errors == ["Frontfy: not in config/boards.yaml — check the spelling, or give its ats and slug"]


def test_a_full_entry_for_a_board_the_catalogue_lacks_is_scanned_as_written():
    result = _resolve([{"name": "Acme", "ats": "Greenhouse", "slug": "acme"}])
    assert result.boards == [{"name": "Acme", "ats": "greenhouse", "slug": "acme", "origin": "local"}]
    assert result.notes == []


def test_a_full_entry_matching_the_catalogue_is_silent():
    result = _resolve([{"name": "Frontify", "ats": "ashby", "slug": "frontify"}])
    assert result.boards[0]["origin"] == "override"
    assert result.notes == []


def test_an_override_that_disagrees_with_the_catalogue_is_scanned_but_warned_about():
    # The stale-copy case the shared list exists for: the user's entry still
    # points at the board the company left.
    result = _resolve([{"name": "Frontify", "ats": "lever", "slug": "frontify"}])
    assert result.boards == [{"name": "Frontify", "ats": "lever", "slug": "frontify", "origin": "override"}]
    assert len(result.warnings) == 1
    assert "ashby/frontify" in result.warnings[0] and "lever/frontify" in result.warnings[0]


def test_half_an_override_is_an_error():
    result = _resolve([{"name": "Acme", "ats": "greenhouse"}])
    assert result.boards == []
    assert result.errors == ["Acme: give both ats and slug, or neither (uncomment all of its lines)"]


def test_an_unknown_ats_is_an_error():
    result = _resolve([{"name": "Acme", "ats": "myspace", "slug": "acme"}])
    assert result.boards == []
    assert result.errors == ["Acme: unknown ats 'myspace'"]


def test_a_name_listed_twice_is_scanned_once():
    result = _resolve(["Frontify", "FRONTIFY"])
    assert len(result.boards) == 1
    assert result.warnings == ["FRONTIFY: listed twice; scanned once"]


def test_entries_that_are_neither_names_nor_blocks_are_errors():
    result = _resolve([42, {"ats": "ashby", "slug": "x"}])
    assert result.boards == []
    assert result.errors == [
        "entry 1 is neither a name nor a name/ats/slug block",
        "entry 2: missing name",
    ]


def test_one_bad_entry_costs_only_itself():
    result = _resolve(["Frontify", "Nope", "Huawei (Zurich)"])
    assert [b["name"] for b in result.boards] == ["Frontify", "Huawei (Zurich)"]
    assert result.selected == 3


# --- the catalogue -------------------------------------------------------------


def test_catalogue_problems_name_incomplete_unknown_and_duplicate_entries():
    problems = catalog_problems([
        {"name": "Acme", "ats": "greenhouse"},
        {"name": "Beta", "ats": "myspace", "slug": "beta"},
        {"name": "Gamma", "ats": "lever", "slug": "gamma"},
        {"name": "gamma", "ats": "lever", "slug": "gamma2"},
        "Delta",
    ])
    assert problems == [
        "Acme: missing slug",
        "Beta: unknown ats 'myspace'",
        "gamma: listed twice (also as 'Gamma')",
        "entry 5 is not a name/ats/slug block",
    ]


def test_incomplete_catalogue_entries_are_left_out_of_the_index():
    index = index_catalog([{"name": "Acme", "ats": "greenhouse"}, *_CATALOG])
    assert sorted(index) == ["frontify", "huawei (zurich)"]


# --- reading the files ---------------------------------------------------------


def _root(tmp_path, *, boards_yaml, companies_yaml):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "boards.yaml").write_text(boards_yaml, encoding="utf-8")
    (tmp_path / "config" / "companies.yaml").write_text(companies_yaml, encoding="utf-8")
    return tmp_path


def test_load_companies_resolves_the_selection_against_the_catalogue(tmp_path):
    root = _root(
        tmp_path,
        boards_yaml="boards:\n  - name: Frontify\n    ats: ashby\n    slug: frontify\n",
        companies_yaml="companies:\n  - Frontify\n  - name: Acme\n    ats: lever\n    slug: acme\n",
    )
    result = load_companies(root)
    assert [(b["name"], b["origin"]) for b in result.boards] == [("Frontify", "catalog"), ("Acme", "local")]


def test_a_selection_with_everything_commented_out_is_empty(tmp_path):
    root = _root(
        tmp_path,
        boards_yaml="boards:\n  - name: Frontify\n    ats: ashby\n    slug: frontify\n",
        companies_yaml="companies:\n  # - Frontify\n",
    )
    result = load_companies(root)
    assert result.boards == [] and result.notes == [] and result.selected == 0


def test_orphaned_lines_merging_into_the_entry_above_are_refused(tmp_path):
    # The Swiss Re trap: comment out a `- name:` line but not its ats/slug, and
    # PyYAML silently hands them to the previous entry.
    root = _root(
        tmp_path,
        boards_yaml="boards: []\n",
        companies_yaml=(
            "companies:\n"
            "  - name: Swiss Re\n    ats: successfactors\n    slug: careers.swissre.com\n"
            "  # - name: Jobgether\n    ats: lever\n    slug: jobgether\n"
        ),
    )
    with pytest.raises(BoardsError, match="appears twice"):
        load_companies(root)


@pytest.mark.parametrize("missing", ["boards.yaml", "companies.yaml"])
def test_a_missing_file_is_an_error(tmp_path, missing):
    root = _root(tmp_path, boards_yaml="boards: []\n", companies_yaml="companies: []\n")
    (root / "config" / missing).unlink()
    with pytest.raises(BoardsError, match=missing):
        load_companies(root)


def test_a_file_without_its_top_level_list_is_an_error(tmp_path):
    root = _root(tmp_path, boards_yaml="boards: []\n", companies_yaml="companies: Frontify\n")
    with pytest.raises(BoardsError, match="must be a list"):
        load_companies(root)



def test_the_shipped_catalogue_is_well_formed():
    # Every entry complete, on a known ATS, and listed once.
    assert catalog_problems(read_catalog(_ROOT / "config" / "boards.yaml")) == []

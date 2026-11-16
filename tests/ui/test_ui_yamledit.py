import pytest
import yaml

from jobradar.ui import yamledit
from jobradar.ui.yamledit import Unsupported

TEXT = """\
# Head comment.
thresholds:
  # The floor.
  min_skill: 60   # [60]
  note: "a # in quotes"  # kept

output:
  max_best: 1

names:
    - "Alpha"   # first
    # between
    - Beta
    - name: Own
      slug: own   # theirs

    # a rule before what follows
tail: true
"""


def test_scalar_is_replaced_on_its_line_with_its_comment():
    out = yamledit.set_scalar(TEXT, ("thresholds", "min_skill"), 75)
    assert out == TEXT.replace("min_skill: 60   # [60]", "min_skill: 75   # [60]")
    out = yamledit.set_scalar(TEXT, ("thresholds", "note"), "plain")
    assert 'note: plain  # kept' in out


@pytest.mark.parametrize("value, written", [(None, "null"), (True, "true"), (0, "0"), ("a: b", "'a: b'")])
def test_scalar_values_are_written_as_yaml(value, written):
    out = yamledit.set_scalar(TEXT, ("output", "max_best"), value)
    assert f"  max_best: {written}\n" in out
    assert yaml.safe_load(out)["output"]["max_best"] == value


def test_a_missing_key_is_added_to_its_section_and_a_missing_section_to_the_file():
    out = yamledit.set_scalar(TEXT, ("output", "max_okay"), 3)
    assert "output:\n  max_best: 1\n  max_okay: 3\n" in out
    out = yamledit.set_scalar(TEXT, ("rav", "enabled"), True)
    assert out.endswith("tail: true\n\nrav:\n  enabled: true\n")
    assert yamledit.set_scalar("", ("top",), 1) == "top: 1\n"


def test_a_key_of_the_same_name_in_another_section_is_not_touched():
    text = "a:\n  enabled: false\nb:\n  enabled: false\n"
    assert yamledit.set_scalar(text, ("b", "enabled"), True) == "a:\n  enabled: false\nb:\n  enabled: true\n"


def test_list_items_are_dropped_and_appended_around_what_stays():
    out = yamledit.set_list(TEXT, ("names",), ["beta", "Gamma"], same=str.lower)
    assert out == TEXT.replace('    - "Alpha"   # first\n', "").replace(
        "      slug: own   # theirs\n", "      slug: own   # theirs\n    - Gamma\n"
    )
    assert yaml.safe_load(out)["names"] == ["Beta", {"name": "Own", "slug": "own"}, "Gamma"]


def test_a_mapping_item_goes_only_when_told_to():
    out = yamledit.set_list(TEXT, ("names",), ["Alpha", "Beta"], keep_entry=lambda entry: False)
    assert "Own" not in out and "    # a rule before what follows\n" in out
    assert yaml.safe_load(out)["names"] == ["Alpha", "Beta"]


def test_an_emptied_list_reads_as_empty_not_as_nothing():
    out = yamledit.set_list(TEXT, ("names",), [], keep_entry=lambda entry: False)
    assert yaml.safe_load(out)["names"] == []
    assert "    # between\n" in out and yaml.safe_load(out)["tail"] is True


@pytest.mark.parametrize("text", [
    "names: []\n", "names:\n", "names: null  # none yet\n", "other: 1\n", "names:\n  # - Old\n",
])
def test_a_list_that_has_no_items_yet_gets_them(text):
    out = yamledit.set_list(text, ("names",), ["A (b)", "C: d"])
    assert yaml.safe_load(out)["names"] == ["A (b)", "C: d"]
    assert out.count("#") == text.count("#")


def test_an_inline_list_stays_inline():
    out = yamledit.set_list('x:\n  days: [mon, tue, wed]  # weekdays\n', ("x", "days"), ["wed", "fri", "mon"])
    assert out == "x:\n  days: [mon, wed, fri]  # weekdays\n"


def test_a_duplicate_item_is_kept_once():
    out = yamledit.set_list("names:\n  - A\n  - a\n  - B\n", ("names",), ["A"], same=str.lower)
    assert out == "names:\n  - A\n"


@pytest.mark.parametrize("call", [
    lambda: yamledit.set_scalar(TEXT, ("thresholds",), 1),          # a section, not a value
    lambda: yamledit.set_list(TEXT, ("thresholds",), ["a"]),        # a mapping, not a list
    lambda: yamledit.set_list(TEXT, ("output", "max_best"), ["a"]),  # a scalar
    lambda: yamledit.set_scalar(TEXT, ("tail", "deeper"), 1),        # under a scalar
])
def test_shapes_it_does_not_edit_are_refused_not_guessed(call):
    with pytest.raises(Unsupported):
        call()

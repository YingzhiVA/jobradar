from pathlib import Path

from jobradar.matching import load_profile

_ROOT = Path(__file__).resolve().parents[1]


def test_shipped_profile_loads():
    # The template repo must ship at least one CV under profile/cvs/ (the
    # example), or the very first --dry-run dies with "No CVs found".
    cvs, _identity, _stories = load_profile(_ROOT / "profile")
    assert cvs

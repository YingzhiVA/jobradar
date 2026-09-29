"""Deterministic sanity check on the postcode the extraction model guesses.

The model fills in a postcode from its own knowledge when the posting only
names a town, and on multi-location postings it has confused towns (Zug
recorded as 6900, which is Lugano). For the towns Swiss postings name most
often, the location string is checked against a small table: a guess that
belongs to one of the named towns is kept, otherwise the first named town's
postcode replaces it. Towns outside the table keep the model's guess.
"""

from __future__ import annotations

import re
import unicodedata

# (spellings, central postcode, postcode range of the town). Ranges are kept
# to the town itself so a guess from a neighbouring town is not accepted.
_TOWNS: list[tuple[tuple[str, ...], str, int, int]] = [
    (("zurich", "zuerich"), "8001", 8000, 8099),
    (("zug",), "6300", 6300, 6304),
    (("baar",), "6340", 6340, 6340),
    (("rotkreuz",), "6343", 6343, 6343),
    (("basel", "bale"), "4001", 4000, 4059),
    (("kaiseraugst",), "4303", 4303, 4303),
    (("bern", "berne"), "3011", 3000, 3030),
    (("geneva", "geneve", "genf"), "1201", 1200, 1299),
    (("lausanne",), "1003", 1000, 1018),
    (("lugano",), "6900", 6900, 6979),
    (("luzern", "lucerne"), "6003", 6000, 6015),
    (("winterthur",), "8400", 8400, 8411),
    (("st. gallen", "st gallen", "st.gallen", "sankt gallen"), "9000", 9000, 9016),
    (("schaffhausen",), "8200", 8200, 8200),
    (("stafa",), "8712", 8712, 8712),
    (("wallisellen",), "8304", 8304, 8304),
    (("opfikon", "glattbrugg"), "8152", 8152, 8152),
    (("aarau",), "5000", 5000, 5004),
    (("fribourg", "freiburg im uechtland"), "1700", 1700, 1709),
    (("neuchatel", "neuenburg"), "2000", 2000, 2000),
    (("chur",), "7000", 7000, 7000),
]


def _fold(text: str) -> str:
    """Lowercase and strip accents, so Zürich/Zurich and Genève/Geneve match."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _named_towns(location: str) -> list[tuple[str, int, int]]:
    """Table towns named in `location`, in the order they appear."""
    folded = _fold(location)
    hits: list[tuple[int, tuple[str, int, int]]] = []
    for spellings, code, lo, hi in _TOWNS:
        positions = [
            m.start()
            for s in spellings
            if (m := re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", folded))
        ]
        if positions:
            hits.append((min(positions), (code, lo, hi)))
    return [town for _, town in sorted(hits)]


def resolve_postcode(location: str, guess: str) -> str:
    """The postcode to record for a posting at `location`, given the model's
    `guess`: the guess when it matches a named town (or no table town is
    named), else the first named town's central postcode."""
    guess = guess.strip()
    towns = _named_towns(location)
    if not towns:
        return guess
    if guess.isdigit() and any(lo <= int(guess) <= hi for _, lo, hi in towns):
        return guess
    return towns[0][0]

"""The country a jobradar instance searches in, and everything that follows from it.

The pipeline is country-neutral; what is not lives here, one ``Market`` per
country: the country's names, its regions (Swiss cantons) and the towns that
name one unambiguously, the wording the prompts use for them, the writing rules
for application documents, and whether the country has an unemployment-office
report the apply CLI can render (Swiss RAV).

``config/search.yaml`` picks one with ``market:`` (default ``ch``); the search,
discovery and apply entry points read it and pass the ``Market`` down. Functions
below them take it as a ``market`` argument defaulting to ``CH``, which is the
only market so far.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    code: str
    # The country's English name, as the prompts write it and as the country
    # facets of global ATS boards (Workday, SuccessFactors, iCIMS, Google's
    # feed) spell it.
    country: str
    # ISO 3166-1 alpha-2, upper case: what Eightfold's locations end in and
    # (lower-cased) what onlyfy's country filter takes.
    country_code: str
    # Every spelling of the country a location line may use, lower case, the
    # English name first.
    country_aliases: tuple[str, ...]
    # "Swiss office", "Swiss canton".
    adjective: str
    # The canonical region names a posting's resolved region is one of.
    regions: tuple[str, ...]
    # Region word as the web-search location brief uses it ("cantons").
    regions_word: str
    # Towns that name their region unambiguously, lower case -> region. Not
    # exhaustive: anything else is left to the gap-fill model.
    region_by_city: Mapping[str, str]
    # The `canton` and `canton_source` lines of the gap-fill extraction prompt,
    # with a {regions} placeholder for the comma-joined region names.
    region_prompt: str
    # Writing rules per ISO 639-1 language code, for the languages that have a
    # local variety to get wrong. A code not here gets none.
    writing_rules_by_language: Mapping[str, str]
    # The same rules, condensed, for when only the model knows the language.
    writing_rules_any: str
    # Deterministic spelling fixes per language-code prefix, applied after
    # generation: (old, new) pairs.
    spelling_fixes: Mapping[str, tuple[tuple[str, str], ...]]
    # Whether the apply CLI's unemployment-office report (--rav) applies.
    rav: bool

    def region_in(self, location_text: str) -> str | None:
        """The region of the first unambiguous town named in `location_text`."""
        haystack = location_text.lower()
        for city, region in self.region_by_city.items():
            if re.search(rf"\b{re.escape(city)}\b", haystack):
                return region
        return None

    def mentions_country(self, text: str) -> bool:
        haystack = text.lower()
        return any(alias in haystack for alias in self.country_aliases)

    def writing_rules(self, code: str) -> str:
        """Prompt text for writing `code`'s local variety, or "" for a language
        with none to get wrong."""
        return self.writing_rules_by_language.get((code or "").lower(), "")

    def respell(self, text: str, code: str) -> str:
        """Apply the deterministic spelling fixes for `code`'s language."""
        code = (code or "").lower()
        for prefix, fixes in self.spelling_fixes.items():
            if code.startswith(prefix):
                for old, new in fixes:
                    text = text.replace(old, new)
        return text


from .ch import CH  # noqa: E402 - ch.py imports Market from this module

MARKETS: dict[str, Market] = {m.code: m for m in (CH,)}
DEFAULT_MARKET = CH

# Every country spelling any market knows, keyed by the English name in lower
# case, so remote_countries: ["Switzerland"] also matches "Schweiz" whatever
# market is selected.
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    m.country.lower(): m.country_aliases for m in MARKETS.values()
}

__all__ = ["CH", "COUNTRY_ALIASES", "DEFAULT_MARKET", "MARKETS", "Market"]

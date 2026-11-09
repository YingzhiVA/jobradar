from dataclasses import replace

import pytest

from jobradar import config as config_mod
from jobradar.config import ConfigError, SearchSettings, load_search_settings
from jobradar.markets import CH, COUNTRY_ALIASES, DEFAULT_MARKET, MARKETS

from .test_config import write_config


# --- the Swiss market ----------------------------------------------------------


def test_ch_is_the_default_and_only_market():
    assert DEFAULT_MARKET is CH
    assert MARKETS == {"ch": CH}


def test_ch_resolves_unambiguous_towns_to_their_canton():
    assert CH.region_in("Zürich, Switzerland") == "Zurich"
    assert CH.region_in("Staefa") == "Zurich"
    assert CH.region_in("Risch-Rotkreuz, Zug") == "Zug"
    # A town the table doesn't know is left to the gap-fill model.
    assert CH.region_in("Cham, Switzerland") is None
    # Whole words only: "Zugspitze" is not Zug.
    assert CH.region_in("Zugspitze") is None


def test_every_town_maps_to_a_known_canton():
    assert set(CH.region_by_city.values()) <= set(CH.regions)
    assert len(CH.regions) == 26


def test_ch_recognises_the_country_in_any_national_language():
    for text in ("Remote, Switzerland", "Schweiz", "Genève, Suisse", "Lugano, Svizzera"):
        assert CH.mentions_country(text)
    assert not CH.mentions_country("Berlin, Germany")


def test_country_aliases_cover_every_market():
    assert COUNTRY_ALIASES["switzerland"] == CH.country_aliases


def test_region_prompt_names_every_canton():
    prompt = CH.region_prompt.format(regions=", ".join(CH.regions))
    assert "Appenzell Innerrhoden" in prompt
    assert "{" not in prompt


# Moved from tests/apply/test_apply_translation.py with the rules themselves.


def test_swiss_spelling_replaces_eszett_in_german():
    assert CH.respell("Die größte Straße", "de") == "Die grösste Strasse"
    assert CH.respell("ẞBUNG", "de") == "SSBUNG"
    assert CH.respell("Grösse", "de") == "Grösse"
    assert CH.respell("Straße", "de-CH") == "Strasse"


def test_swiss_spelling_leaves_other_languages_alone():
    # Not German, so nothing to enforce - and no other language uses the letter.
    assert CH.respell("groß", "fr") == "groß"
    assert CH.respell("groß", "en") == "groß"
    assert CH.respell("groß", "") == "groß"


def test_swiss_conventions_only_for_languages_with_a_swiss_variety():
    assert "ß" in CH.writing_rules("de")
    assert "septante" in CH.writing_rules("fr")
    assert CH.writing_rules("en") == ""
    assert CH.writing_rules("it") == ""
    assert CH.writing_rules("") == ""


# --- the market setting --------------------------------------------------------


def test_market_defaults_to_ch(tmp_path):
    assert load_search_settings(tmp_path).market is CH


@pytest.mark.parametrize("value", ["ch", "CH", " ch "])
def test_market_accepts_a_known_code(tmp_path, value):
    write_config(tmp_path, {"market": value})
    assert load_search_settings(tmp_path).market is CH


def test_market_rejects_an_unknown_code(tmp_path):
    write_config(tmp_path, {"market": "de"})
    with pytest.raises(ConfigError, match="market: 'de' is not a known market"):
        load_search_settings(tmp_path)


def test_rav_is_refused_in_a_market_without_it(monkeypatch):
    no_rav = replace(CH, code="xx", country="Elsewhere", rav=False)
    monkeypatch.setitem(config_mod.MARKETS, "xx", no_rav)
    assert SearchSettings.from_dict({"market": "xx"}).market is no_rav
    with pytest.raises(ConfigError, match="rav.enabled"):
        SearchSettings.from_dict({"market": "xx", "rav": {"enabled": True}})


def test_describe_names_the_market():
    assert "market:            ch (Switzerland)" in SearchSettings().describe()

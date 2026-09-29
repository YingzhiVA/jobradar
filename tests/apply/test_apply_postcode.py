from jobradar.apply.postcode import resolve_postcode


def test_wrong_town_guess_is_replaced():
    # The observed failure: Zug recorded with Lugano's postcode.
    assert resolve_postcode("Zug, Switzerland", "6900") == "6300"


def test_multi_location_posting_uses_the_swiss_town():
    location = "Beerse / Ringaskiddy / Latina / Leiden / Zug / Belgium / Ireland"
    assert resolve_postcode(location, "6900") == "6300"
    assert resolve_postcode("Lisbon - Lisboa - Portugal; Zug - Zug - Switzerland", "") == "6300"


def test_guess_matching_any_named_town_is_kept():
    assert resolve_postcode("Basel, Zurich, Geneva", "8001") == "8001"
    assert resolve_postcode("Zürich", "8050") == "8050"


def test_accents_and_spellings_fold():
    assert resolve_postcode("Genève", "") == "1201"
    assert resolve_postcode("St. Gallen", "8001") == "9000"


def test_unknown_town_keeps_the_guess():
    assert resolve_postcode("Buchs SG", "9470") == "9470"
    assert resolve_postcode("", "8001") == "8001"
    assert resolve_postcode("Remote", "") == ""


def test_town_names_match_whole_words_only():
    # "Bern" inside "Bernese" or "Zug" inside "Zugang" must not count.
    assert resolve_postcode("Bernese Oberland", "3800") == "3800"
    assert resolve_postcode("Zugang flexibel", "8001") == "8001"

"""Switzerland."""

from __future__ import annotations

from . import Market

# The 26 Swiss cantons, canonical English names — used both as the
# heuristic dict's output vocabulary and as guidance for the LLM fallback,
# so posting.canton is always one of these regardless of which path filled it.
CANTONS = (
    "Zurich", "Bern", "Lucerne", "Uri", "Schwyz", "Obwalden", "Nidwalden",
    "Glarus", "Zug", "Fribourg", "Solothurn", "Basel-Stadt", "Basel-Landschaft",
    "Schaffhausen", "Appenzell Ausserrhoden", "Appenzell Innerrhoden",
    "St. Gallen", "Graubünden", "Aargau", "Thurgau", "Ticino", "Vaud",
    "Valais", "Neuchâtel", "Geneva", "Jura",
)

# Only the unambiguous cases: a major city/town whose name we're confident
# identifies its canton without needing the LLM. Deliberately not
# exhaustive — anything not in here (most towns, e.g. within Vaud, Aargau,
# Ticino, Basel-Landschaft) falls through to the LLM fallback in
# normalize.llm_fill_gaps(), which has reliable general knowledge of Swiss
# geography without this module having to hand-maintain ~2000 municipality
# mappings.
CANTON_BY_CITY = {
    "zurich": "Zurich", "zürich": "Zurich", "winterthur": "Zurich",
    "bern": "Bern", "berne": "Bern",
    "lucerne": "Lucerne", "luzern": "Lucerne",
    "zug": "Zug",
    # Risch-Rotkreuz: not a major town, but it's Roche Diagnostics' site and the
    # only place the Roche board is scoped to, so it recurs daily — worth the
    # entry to skip an LLM call per posting. Unambiguous: one municipality.
    "rotkreuz": "Zug",
    # Kaiseraugst: same reasoning — Roche's second Swiss site, ~20 postings a
    # day since the board was widened to it (2026-09-07). One municipality.
    "kaiseraugst": "Aargau",
    # Stäfa: Sonova's headquarters and the site of nearly all its Swiss
    # postings (~18 a day since 2026-09-07), which its board spells "Staefa".
    # One municipality.
    "stäfa": "Zurich", "staefa": "Zurich",
    "schaffhausen": "Schaffhausen",
    "solothurn": "Solothurn",
    "glarus": "Glarus",
    "schwyz": "Schwyz",
    "geneva": "Geneva", "genève": "Geneva", "geneve": "Geneva",
    # Lausanne: the board health check found Neural Concept and Corintis read
    # as posting nowhere in Switzerland without it (2026-11-09).
    "lausanne": "Vaud",
    "neuchâtel": "Neuchâtel", "neuchatel": "Neuchâtel",
    "fribourg": "Fribourg", "freiburg": "Fribourg",
    "st. gallen": "St. Gallen", "st gallen": "St. Gallen", "saint gallen": "St. Gallen",
    "sankt gallen": "St. Gallen",
    "basel": "Basel-Stadt",
}

_REGION_PROMPT = """\
- canton: which Swiss canton THIS JOB is in, worked out from the town/city on \
the Location line and from that line alone (e.g. Lausanne -> Vaud, Lugano -> \
Ticino). Descriptions routinely name other places in passing — the company's \
headquarters, its other offices, where its customers are — and none of those \
say anything about where this job is, so never read the canton off the \
description. A Location line outside Switzerland is null even when the \
description says the company is Swiss: Location "London Area" is null, no \
matter how prominently the description mentions a Swiss head office. Only \
when no Location line is given at all, fall back to a job location the \
description states explicitly. Use exactly one of these names: {regions}. \
Null if the job isn't in Switzerland or the location is too vague to place.
- canton_source: the exact substring of the Location line you read the canton \
off, copied verbatim (Location "Cham, Switzerland" -> "Cham"). Null when \
canton is null, and null when you fell back to the description because no \
Location line was given."""

# Swiss German and Swiss French are not the German of Germany and the French
# of France, and every posting this pipeline sees is Swiss. A reviewer in
# Zurich reads a stray ß-rule violation the way a reviewer in Berlin would
# read "colour": not wrong exactly, just written by someone from elsewhere.
_WRITING_RULES = {
    "de": """\
Write Swiss Standard German (Schweizer Hochdeutsch), not the German of
Germany:
- The letter ß does not exist in Swiss orthography. Always write ss:
  Strasse, grösste, Mass, dass. Never use it, not even in quotations.
- Quote with guillemets «so», not with German quotation marks.
- Group thousands with an apostrophe: CHF 120'000, not 120.000.
- Swiss business vocabulary where it differs: Lohn (not Gehalt),
  Stelleninserat (not Stellenanzeige), Mitarbeitende. Dates as 06.08.2026.""",
    "fr": """\
Write Swiss French (français de Suisse romande), not the French of France:
- septante for 70 and nonante for 90, never soixante-dix or quatre-vingt-dix.
  Leave 80 as quatre-vingts unless the posting itself writes huitante.
- Quote with guillemets « so », a space inside each mark.
- Group thousands with an apostrophe: CHF 120'000.
- Swiss vocabulary where it differs: place de travail, formation continue.
  Dates as 06.08.2026.""",
}

# Used when the language is only known to the model, not to the caller.
_WRITING_RULES_ANY = """\
Every posting here is Swiss. If that language is German or French, write the
Swiss variety: Swiss Standard German has no ß (write ss - Strasse, grösste,
dass), Swiss French uses septante and nonante, and both quote with guillemets
and group thousands with an apostrophe (CHF 120'000)."""

CH = Market(
    code="ch",
    country="Switzerland",
    country_code="CH",
    country_aliases=("switzerland", "schweiz", "suisse", "svizzera"),
    adjective="Swiss",
    regions=CANTONS,
    regions_word="cantons",
    region_by_city=CANTON_BY_CITY,
    region_prompt=_REGION_PROMPT,
    writing_rules_by_language=_WRITING_RULES,
    writing_rules_any=_WRITING_RULES_ANY,
    # The one Swiss rule a model trained mostly on German German keeps
    # breaking. The prompt makes it likely; this makes it certain. It also
    # Swiss-ifies a German name carrying an ß, which is the trade Swiss usage
    # itself makes (Swiss papers write Straßburg as Strassburg).
    spelling_fixes={"de": (("ß", "ss"), ("ẞ", "SS"))},
    rav=True,
)

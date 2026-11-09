# Developing

## Tests

```bash
./.venv/bin/python -m pytest
```

No API key is needed. The suite covers the deterministic pieces —
`search/filters.py`, `search/ranking.py`, `search/normalize.py`'s heuristics,
the config loader, the apply bookkeeping (tracker, lint, archive, PDF
conversion) — the parts that should never produce a different answer for the
same input. Model calls are faked where a test needs one.

## Layout

```
src/jobradar/
  config.py        config/search.yaml: dials and the market / rav / sources switches
  doctor.py        python -m jobradar.doctor — setup checks
  llm.py           which model each stage uses (env overrides)
  markets/         the country searched: its regions, prompt wording, writing rules
  matching.py      profile loading, the scoring call
  models.py        Posting / Constraints / ScoredPosting
  search/          the daily run: main, sources/, filters, ranking, writeup, report, notify
  discovery/       company discovery and its ledger
  apply/           the application pipeline: fetch, extract, tailor, lint, pdf, tracker, archive
  archive.py       retention sweep for reports and the seen store
  schedule.py      is today a run day?
  usage.py         prompt-cache accounting for the logs
```

## Adding a source

`src/jobradar/search/sources/` connectors all implement the same
`fetch() -> FetchResult` shape (`search/sources/base.py`). To add another
applicant-tracking system, write a `_fetch_<ats>` function in
`company_pages.py` and register it in `_FETCHERS`; for a whole new source,
follow the pattern in `eth.py` and wire it into `_build_sources()` in
`search/main.py`. A source that needs a user decision (like the ETH board's
categories) gets a switch in `config/search.yaml`, see `Sources` in
`config.py`.

If the board only gives a posting's description on its own page, so your
connector makes one request per posting, it also has to skip postings already
seen: accept a `known: KnownPostings` argument, add the ATS to `_SKIPS_KNOWN`,
and before each detail request return `_listing_only(...)` when `known` holds
the posting's URL. Use the stored URL `known` gives back rather than one you
build, because a posting's identity is a hash of its URL. A connector that
skips this still works and passes its tests; it just re-downloads every
posting it has ever seen on every run, which is how a run once took an hour.
The existing per-posting connectors in `company_pages.py` show the three
lookups (`exact`, `by_segment`, `by_leading_id`).

A board that lists postings from many countries should ask for the market's
country only, before any per-posting request: accept a `market: Market`
argument, add the ATS to `_SCOPED_TO_MARKET`, and pass `market.country` (or
`market.country_code`) to the board's own location filter. Workday,
SuccessFactors and iCIMS do this.

## Country-specific code

Anything that depends on the country searched belongs in `src/jobradar/markets/`,
not in the pipeline: the country's names, its regions (Swiss cantons) and the
towns that name one, the gap-fill prompt's region instructions, the writing
rules for application documents, and whether the RAV report applies. The
pipeline takes a `Market` and reads these off it. `config/search.yaml` selects
one with `market:` (default `ch`, the only one so far).

## Reading the run log

`reports/runs.jsonl` has one JSON line per run. `search/observability.py`
documents the record shape and carries `jq` one-liners for the questions it
was built to answer: which queries web search chose, how the funnel narrowed,
what happened to each scored posting, and why each unscored one was dropped.

## Contributing

Issues and pull requests are welcome, especially new board connectors and
corrections to the company list. Keep personal data out of the repo: tests
use fictional people and companies, and nothing under `profile/`,
`applications/`, `reports/` or `data/` belongs in a pull request.

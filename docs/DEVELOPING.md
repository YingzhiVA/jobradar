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
  config.py        config/search.yaml: dials and the rav / sources switches
  doctor.py        python -m jobradar.doctor — setup checks
  llm.py           which model each stage uses (env overrides)
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

## Reading the run log

`reports/runs.jsonl` has one JSON line per run. `search/observability.py`
documents the record shape and carries `jq` one-liners for the questions it
was built to answer: which queries web search chose, how the funnel narrowed,
what happened to each scored posting.

## Contributing

Issues and pull requests are welcome, especially new board connectors and
corrections to the company list. Keep personal data out of the repo: tests
use fictional people and companies, and nothing under `profile/`,
`applications/`, `reports/` or `data/` belongs in a pull request.

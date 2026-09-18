# The daily search

`python -m jobradar.search.main` is one complete run: fetch, filter, score,
rank, write up, report. This page covers how to steer it and what it does.

## Configure

1. **`config/companies.yaml`** — the career pages to scan. **Nothing is
   scanned until you switch boards on**: the file ships as a catalogue of 67
   Swiss boards (AI, data, medtech, insurance, pharma, consulting), every one
   commented out, with each board's connector quirks noted next to it. Remove
   the `# ` from all three lines of each board you want, then run
   `python -m jobradar.doctor`, which catches an entry uncommented only in
   part. Begin with a handful: the first run over a board scores every
   posting currently open on it (see [COSTS.md](COSTS.md#your-first-run)).
   See [DISCOVERY.md](DISCOVERY.md) for finding boards that aren't listed.
2. **`config/constraints.yaml`** — hard requirements, applied as pure code
   before any model call. For Switzerland, prefer `allowed_cantons` over
   `allowed_cities`: postings usually name a town rather than the canton, so
   a city list misses any town you didn't list, while canton matching catches
   the whole canton. Do not also set `allowed_countries: ["Switzerland"]`
   alongside it: the checks are OR'd, so the country match would let every
   Swiss posting through regardless of canton. `remote_countries` is the
   narrow way to accept remote roles scoped to a country.
3. **`config/search.yaml`** — how the search is steered. Every key is
   optional and ships at its default, so the file doubles as its own
   documentation:

   | Key | Default | What it does |
   | --- | --- | --- |
   | `thresholds.min_skill` | `60` | Skill floor (0–100). Below it a posting is dropped **for good** (marked seen). Lower it to see more in a thin market. |
   | `thresholds.min_interest` | `0` (off) | Interest floor. Off on purpose: eligibility gates on skill alone, so a role you're qualified for but lukewarm about still surfaces. Raise it to see fewer, more motivating roles. |
   | `thresholds.best_threshold` | `80` | The bar for the headline "best" tier, on the *combined* (mean) score, so a promotion needs genuine interest as well as skill. |
   | `output.max_best` | `1` | Headline matches per run. |
   | `output.max_okay` | `3` | Secondary matches per run (daily total = `max_best + max_okay`). |
   | `schedule.frequency` | `daily` | `daily` / `every_other_day` / `twice_weekly` / `weekly` / `custom` → at least 1 / 2 / 3 / 7 / `min_interval_days` days between runs. |
   | `schedule.days_of_week` | `[mon…fri]` | Days the search may run at all. `[]` means any day. |
   | `rav.enabled` | `false` | Swiss RAV registrants: turns on the monthly proof-of-applications table and the archive gate that keeps submitted applications until their month is filed. See [APPLY.md](APPLY.md). |
   | `sources.eth_jobs.enabled` | `false` | Also scan ETH Zurich's own job board, by category. |
   | `sources.eth_jobs.job_types` | `[management, administration]` | Which of the board's job-type categories to scan; the board's numeric ids work too. |

   After editing, print what the program will actually read:

   ```bash
   ./.venv/bin/python -m jobradar.config
   ```

   A *missing* key (or the whole file) falls back to the default above. A key
   that's present but unusable — a threshold over 100, an unknown frequency —
   fails the run with a message naming it, rather than reverting silently and
   searching wrong for days.

   Raising the caps later doesn't mean the backlog is lost: eligible postings
   that lost the cap are left unmarked and resurface on the next run (the run
   log labels them `deferred-capped`). Lowering a *floor* does re-open ground,
   but postings already dropped below the old floor were marked seen and won't
   come back unless you reset `data/seen_postings.json`.

## Run

```bash
# Validate the pipeline against seeded fixture postings before touching
# live sources or your dedup history. Still makes real LLM calls.
./.venv/bin/python -m jobradar.search.main --dry-run

# A real run against your configured sources.
./.venv/bin/python -m jobradar.search.main

# Skip the web_search fallback/discovery source (faster, fewer calls).
./.venv/bin/python -m jobradar.search.main --no-web-search

# For a machine-local cron that fires daily: exit without running unless
# config/search.yaml's schedule says today is a run day. A run you type
# yourself is always deliberate, so this is off by default.
./.venv/bin/python -m jobradar.search.main --respect-schedule
```

Before doing anything else, a run checks that your Anthropic credentials
actually work (a free metadata call) and exits immediately with a clear
error if not — otherwise a broken key would silently degrade every model call
into a per-posting warning and produce a misleadingly normal-looking "no
matches today" report.

Each run writes `reports/YYYY-MM-DD.md` (the full report) and
`reports/latest.json` (a short summary), and tracks seen postings in
`data/seen_postings.json`. Postings that were fully processed but didn't make
the daily selection are left unmarked if they cleared both scoring floors —
they'll resurface on a future run rather than being permanently dismissed.
Everything else (ineligible postings, filter failures) is marked seen
immediately so model costs aren't re-paid for the same dead-end result.
Delete that JSON file to reset history (e.g. after changing your CVs enough
that old scores would look different now).

Each run also appends one JSON line to `reports/runs.jsonl`, an
operator-facing trail for auditing how a run behaved: the query strings
`web_search` chose (otherwise unobservable, since the search runs
server-side), each source's health and funnel, the stage-by-stage counts, and
the outcome of every scored posting. See `search/observability.py` for the
record shape and `jq` examples.

## How a run works

1. **Fetch** — pull postings from the boards switched on in
   `config/companies.yaml`, the ETH job board if enabled, and (unless
   `--no-web-search`) a profile-driven web-search discovery query.
2. **Corroborate** (`search/corroborate.py`) — check each web-search lead
   against the employer's own listing, if this run happened to fetch one. A
   lead is dropped only when two independent weak signals agree: the liveness
   check couldn't confirm the link *and* the employer's listing has no such
   role. Either alone isn't enough (bot protection is routine; an ATS listing
   can be paginated short), so either alone only annotates.
3. **Dedup** — drop anything already in `data/seen_postings.json`.
4. **Normalize** — fill in structured fields (workload %, office days, remote,
   sponsorship, Swiss canton) via cheap regex/dict heuristics first, falling
   back to one small Haiku call per posting only when something
   filter-relevant is still missing.
5. **Hard filters** (`search/filters.py`) — pure code, no model: drop
   anything failing `config/constraints.yaml`.
6. **Score** (`matching.py`) — one Haiku call per surviving posting, with your
   CV(s) and identity statement sent as a cached system-prompt prefix,
   returning a skill score, an interest score, which CV fits best, and a
   one-line reason.
7. **Rank & select** (`search/ranking.py`) — the best match (if any clears the
   high bar) plus a few "okay" ones (lower bar); zero is a valid result. All
   four dials come from `config/search.yaml`.
8. **Write-up** (`search/writeup.py`) — for just the selected few, one Sonnet
   call each for an application-ready rationale.
9. **Report** (`search/report.py`) — renders the Markdown report and JSON
   summary. A match whose link nothing could confirm is still open is flagged
   in place, so an unverified link never reads like a checked one.

## Models

Bulk scoring runs on Haiku 4.5; the few finalist write-ups on Sonnet 4.6,
where reasoning and writing quality matter more. The model split is about
fit-to-task, not squeezing cost. Override per stage in `.env`
(`JOBRADAR_SCORING_MODEL`, `JOBRADAR_WRITEUP_MODEL`, `JOBRADAR_WEB_SEARCH_MODEL`);
the values are read when the stage runs, so `.env` is honoured. See
[COSTS.md](COSTS.md) for what each stage spends.

# What it costs

jobradar itself is free. What you pay is Anthropic's per-token price for what
the tool reads (postings, your profile) and writes (scores, rationales,
application drafts), plus a small per-search fee for the web search tool.
All prices in USD.

## See and cap your spend

1. In the [Claude Console](https://platform.claude.com/), create an API key
   just for this tool and name it `jobradar`. The usage page then shows its
   spend on its own line.
2. Set a **monthly spend limit** on the key or the workspace. It is a hard
   cap: a runaway day cannot cost more than you chose.
3. Every run logs a per-stage token summary (`scoring cache: 58 calls |
   … cache-read …`) so you can see the prompt cache doing its work; if
   `cache-read` stays at 0 across a run, the profile block is being re-sent
   at full price and something is off.

## Where the money goes

The default models are Haiku 4.5 for bulk work and Sonnet 4.6 for the few
things that need to be well written. Prices as of September 2026: Haiku $1
per million input tokens, $5 per million output, cache reads at a tenth of
input; Sonnet $3 / $15, cache reads at $0.30. Web search $10 per 1,000
searches, and what it retrieves is billed as input.

The design keeps the expensive parts small: your CVs and identity are sent
once per run as a cached prefix (about 10k tokens), so every extra posting
scored costs only its own text; scoring runs on Haiku; only the handful of
finalists get a Sonnet write-up.

### A daily run

Measured on a typical weekday with the shipped company list: about 110 new
postings fetched, 58 reaching the scorer, 3 surfaced, 5 web searches.

| Stage | Model | About |
| --- | --- | --- |
| Filling in missing fields (workload, canton) | Haiku, ~40 short calls | $0.07 |
| Scoring 58 postings | Haiku, profile cached | $0.20 |
| Write-ups for 3 finalists | Sonnet | $0.09 |
| Web search: 5 searches plus the pages read | Haiku | $0.22 |
| **Total** | | **about $0.55** |

Each additional posting scored adds about a third of a cent. A first run
over a large backlog scales with that; `--no-web-search` removes the largest
single line.

### One application

| Case | About |
| --- | --- |
| Quick tier, English posting, first in a batch | $0.37 |
| Each further application in the same batch (profile cached) | $0.25 |
| A German (or French) posting: translation of the CV on top | + $0.08 |
| `--prep` interview sheet | $0.15–0.20 |
| `--upgrade` to full tier | one more writing call, as above |

The writing call dominates: a tailored CV plus cover letter is about 12k
output tokens on Sonnet, about $0.18 by itself.

### Monthly discovery

One Haiku call with three searches, then plain HTTP probes: about $0.15.

### A typical month

22 weekday runs, 15 applications, one discovery: **$17–25**. Note that a
fresh Anthropic account needs a payment method before it will serve
requests beyond the trial credit.

## The levers

| To spend less | Where |
| --- | --- |
| Skip web search (the biggest single cost) | `--no-web-search`, or `JOBRADAR_WEB_SEARCH_MAX_USES` in `.env` |
| Search less often | `schedule.frequency` in `config/search.yaml` |
| Surface fewer finalists (fewer Sonnet write-ups) | `output.max_best`, `output.max_okay` |
| Score fewer postings | prune `config/companies.yaml`; tighten `config/constraints.yaml` |
| Change a stage's model | `JOBRADAR_SCORING_MODEL`, `JOBRADAR_WRITEUP_MODEL`, `JOBRADAR_APPLY_WRITER_MODEL` … in `.env` |

Prices change; the figures above were measured on 2026-09-17 with
`claude-haiku-4-5` and `claude-sonnet-4-6`. The Console is the source of
truth for what you actually spent.

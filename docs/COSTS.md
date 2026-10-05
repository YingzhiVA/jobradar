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
3. Every run logs a per-stage token summary (`scoring cache: 61 calls |
   … cache-read … | 51400 output`) so you can see the prompt cache doing its
   work and where the tokens go; if `cache-read` stays at 0 across a run, the
   profile block is being re-sent at full price and something is off.

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

### Your first run

A normal day only processes what is new since the day before. The first run
over a board is different: every posting currently open on it is new to you,
so all of those that pass your constraints are scored at once. Adding a board
later costs one such catch-up for that board alone. This is why no board is
selected out of the box.

How much depends less on the number of boards than on how many of their
postings survive your constraints, because the location filter drops
out-of-area postings in code, for free:

- **A large international board can be cheap to add.** When a board with
  4,125 open postings worldwide was added to the author's instance, 80
  reached scoring; the rest were foreign and fell away without a model call.
- **A Swiss-only board is the expensive kind.** Nearly every open posting is
  in Switzerland, so a board with a few hundred open roles puts most of them
  through the model on its first run.

Measured on 2026-09-18: every board in the catalogue except Jobgether, 64 in
all, fetched and run through the free filters with the template's three
cantons, stopping before any model call. Jobgether's own first run is the
international-board example above.

| First run, those 64 boards selected | Postings |
| --- | --- |
| Open on those boards, all of them new to you | 3,726 |
| Reach the paid stages | 1,245 |
| Pass every filter without a model call | 716 |

Adding Jobgether's 80, the whole catalogue comes to roughly **$8–15** in model
calls at the per-call rates above, against about $0.90 for a normal day —
once, and then back to normal. The
Swiss boards dominate: AXA alone put 167 postings through, EY 99, Deloitte
86. With five or ten boards a first run is usually one to two dollars, though
it depends on which: a large Swiss-only board costs more on its own than most.
A wider location setting than the template's three cantons raises every one
of these numbers.

So: select the five or ten boards closest to your field, read a few
reports, then add more a few at a time. `python -m jobradar.doctor` warns
when more than fifteen are selected before your first run.

### A daily run

About **$0.90 a day** for the author's setup of about 65 boards: the average
of four daily runs in late September 2026, as the Claude Console reported it.
The breakdown below is worked out from one run's token counts and comes to a
little more, about $1.03; the day-to-day figure moves with how many postings
reach the scorer. A typical
weekday there fetches about a thousand new postings, most of them from one
large international board and dropped for free by the location filter, with
about 80 reaching the scorer, 3 surfaced and 5 web searches:

| Stage | Model | About |
| --- | --- | --- |
| Filling in missing fields (workload, canton) | Haiku, ~40 short calls | $0.07 |
| Scoring about 80 postings | Haiku, profile cached | $0.65 |
| Write-ups for 3 finalists | Sonnet | $0.09 |
| Web search: 5 searches plus the pages read | Haiku | $0.22 |
| **Total** | | **about $1.03** |

Scoring is the largest line because it returns a checklist, not a number: one
entry per requirement the posting states, each with a verdict, which the skill
score is then computed from. That is about 840 output tokens a posting, and
output is priced at five times input, so it is roughly half the scoring cost.
The figures come from the token counts of a real 61-posting run on 23
September 2026 at the prices below. The earlier single-number scorer cost
about $0.28 for the same 80 postings, and the whole day about $0.66 as the
Console reported it; the difference is almost entirely this line.

About one scored posting in five lands just under the skill floor and is
scored again on up to two later runs before being written off, since a single
draw is not a reliable enough verdict to discard a posting on. A day's run
includes those re-scorings, so the table already accounts for them.

With fewer boards selected the scoring line shrinks; with none, only web
search remains.

Each additional posting scored adds a little under a cent. A first run
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

22 weekday runs, 15 applications, one discovery: **$25–33**. Note that a
fresh Anthropic account needs a payment method before it will serve
requests beyond the trial credit.

## The levers

| To spend less | Where |
| --- | --- |
| Select fewer boards, and add them a few at a time | `config/companies.yaml` |
| Skip web search (the biggest single cost) | `--no-web-search`, or `JOBRADAR_WEB_SEARCH_MAX_USES` in `.env` |
| Search less often | `schedule.frequency` in `config/search.yaml` |
| Surface fewer finalists (fewer Sonnet write-ups) | `output.max_best`, `output.max_okay` |
| Score fewer postings | prune `config/companies.yaml`; tighten `config/constraints.yaml` |
| Change a stage's model | `JOBRADAR_SCORING_MODEL`, `JOBRADAR_WRITEUP_MODEL`, `JOBRADAR_APPLY_WRITER_MODEL` … in `.env` |

Prices change; the figures above were taken on 2026-09-17 (applications,
discovery, web search, write-ups) and 2026-09-23 (scoring), with
`claude-haiku-4-5` and `claude-sonnet-4-6`. The Console is the source of
truth for what you actually spent.

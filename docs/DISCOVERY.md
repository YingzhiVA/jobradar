# Company discovery

Companies you've genuinely chosen to apply to before are good evidence
they're worth actively scanning, not just scoring opportunistically. This
phase builds candidate lists and probes which ones have a supported career
page. It's a setup-time or occasional step, not part of the daily run, and
optional: `config/companies.yaml` already ships a catalogue of 69 Swiss boards,
grouped by industry, that you can switch on by hand.

## Configure

- **`config/seed_companies.txt`** — names you paste from structured
  directories (top100startups.swiss, startup.ch, ETH/EPFL spin-off lists,
  Crunchbase) that the model won't fully recall. One name per line.

## Run

```bash
./.venv/bin/python -m jobradar.discovery.discover

# Skip the model call and probe only the seed names:
./.venv/bin/python -m jobradar.discovery.discover --no-web-search
```

It pulls candidate company names from two sources, then probes each against
the supported applicant-tracking systems (Greenhouse / Lever / Ashby /
Personio / SmartRecruiters / Recruitee / Workable / Teamtailor / BambooHR)
with a handful of common slug guesses:

1. **web search** — Swiss-operating companies that fit your profile (the
   model's recall; excludes already-known names so the budget goes to new
   ones).
2. **`config/seed_companies.txt`** — your pasted directory names.

Workday, Avature, SuccessFactors, iCIMS, BrassRing, Prospective and Google
boards are not probed: their slugs aren't guessable. The header of
`config/companies.yaml` explains how to read each one off a careers URL.

## Review the output

Results are written to `data/discovered_companies.yaml` as **suggestions to
review**; copy the correct ones into `config/companies.yaml` yourself. It
lives in `data/` rather than `config/` because it's generated: every run
replaces the file wholesale, so edits made in place are lost. `config/` holds
only the files you own.

Suggestions are never auto-merged: slug-guessing can land on the wrong
company, and only Greenhouse and SmartRecruiters expose a `company_name` to
sanity-check against (shown as a comment); verify Lever/Ashby/Personio by
opening the URL. A discovery ledger (`data/discovery_ledger.json`) remembers
everything probed so re-runs don't re-suggest or re-probe the same companies
(dropped ones are re-checked after a while).

## Scheduling

Discovery can run in the cloud too: the monthly workflow refreshes the watch
list on the 1st of each month, emails a summary, and commits the suggestions
and ledger back. It reuses the daily workflow's credentials and secrets, so
there's nothing extra to set up; the review step stays manual. See
[CLOUD.md](CLOUD.md).

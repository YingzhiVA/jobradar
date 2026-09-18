# Roadmap

What is deliberately not built yet, and why.

## A simple user interface

The pipeline is driven from the terminal. A small local web page that shows
the latest report and the application tracker, and lets you paste a posting
URL into the queue, would be friendlier, especially for people applying to
non-technical roles. It is deferred on purpose: the hurdles a first-time user
actually hits are getting an API key, writing a CV in Markdown, and setting
up the GitHub schedule, and a page in the browser removes none of them. Those
were lowered first: a setup checker (`python -m jobradar.doctor`), a
copy-paste quick start, an HTML email, and a one-secret cloud setup.

If it gets built, the sketch is: served by Python's standard-library HTTP
server on `localhost`, no framework, no build step; three views (today's
report, the tracker with outcome buttons, the queue with a paste box); every
button maps onto an existing CLI command so there is one implementation of
each action.

## Big-tech career sites

These run their own careers sites instead of a standard applicant-tracking
system, so each needs its own connector. Google (its public job feed) and
NVIDIA (a Workday board) are supported. The rest were checked in September
2026:

- **Microsoft: backlog.** The careers site runs on Eightfold, and robots.txt
  explicitly allows its job API. The search results carry no description, so
  it needs one extra call per posting. Worth writing as a general Eightfold
  connector, since other employers use the same platform.
- **Amazon: backlog.** `amazon.jobs` answers a JSON search that its own site
  uses, and robots.txt allows it. It is undocumented, and Amazon's general
  conditions of use forbid data-gathering robots; whether those cover
  amazon.jobs was not settled.
- **Meta: not planned.** The careers site's robots.txt opens with a notice
  that automated collection is prohibited without Meta's written permission.
  No connector will be added unless that policy changes. Meta roles can still
  surface through web search.

## Other candidates

- More boards: any applicant-tracking system with a public listing is a
  small connector away (see `docs/DEVELOPING.md`).
- A French-first identity template and README, for the Romandie.
- A per-run cost line in the report. Considered and dropped for now: the
  Claude Console already shows spend per API key, and a spend limit there is
  a hard cap, which a printed estimate could never be.

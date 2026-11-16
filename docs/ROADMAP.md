# Roadmap

What is deliberately not built yet, and why.

## An install-free or hosted version

jobradar is for people willing to clone a repo and paste a few commands. The
browser interface ([UI.md](UI.md)) makes the workflow visible and daily use
quicker once that is done; it does not remove the installation, and is not
meant to.

Removing it was tried and dropped. Launchers that fetch Python themselves
worked, but the result was still a ZIP to unpack, a security prompt to get
past, no morning schedule and no way to update. A good experience for someone
who never opens a terminal needs a hosted service: accounts, other people's
CVs and API keys on a server, billing, support. That is a different product
and more than one maintainer can run, so it is not planned.

What may still come to the page: the commands it doesn't wrap yet (company
discovery, archiving, filing a RAV month).

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

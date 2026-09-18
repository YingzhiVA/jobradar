# Running in the cloud

`python -m jobradar.search.main` is a complete, standalone invocation; run it
however you like. The repo ships a daily GitHub Actions schedule
([.github/workflows/daily.yml](../.github/workflows/daily.yml)) so it runs
without your machine being on:

1. It runs the pipeline, then emails the `reports/latest.json` headline and
   matches (only on signal days — matches found or a degraded run; quiet days
   stay silent, override with `JOBRADAR_NOTIFY_ALWAYS=1`). The email has a
   plain-text and an HTML version.
2. It commits the dedup state and the day's report back to the repo, so the
   next run doesn't re-notify you about the same postings. Each cloud run
   starts from a fresh checkout, so this is the only memory it has.
3. It archives old reports and closed applications on the windows in
   `config/retention.yaml`.

A repo made from the template has these workflows already. Until you add
credentials they run for a few seconds, print a setup hint, and exit green.

## Option A: an API key as a repo secret (simplest)

In your private repo: **Settings → Secrets and variables → Actions → New
repository secret**, name `ANTHROPIC_API_KEY`, value your key. That is all.
A key made for this purpose, with a spend limit in the Claude Console, is the
one to use here.

## Option B: no stored key (workload identity federation)

The workflow can instead mint a GitHub OIDC token and let the SDK exchange it
for a short-lived Anthropic access token
([docs](https://platform.claude.com/docs/en/manage-claude/workload-identity-federation)),
so the repo holds nothing that outlives a job. Used only when the key secret
is absent.

One-time setup in the Claude Console (**Settings → Workload identity →
Connect workload → GitHub Actions**): create a service account with the
`developer` organization role, register
`https://token.actions.githubusercontent.com` as an issuer in discovery mode,
and add a rule matching your repo:

```json
{
  "match": {
    "subject_prefix": "repo:<your-github-user>/jobradar:ref:refs/heads/master",
    "audience": "https://api.anthropic.com",
    "claims": { "repository_owner": "<your-github-user>" }
  },
  "oauth_scope": "workspace:developer",
  "token_lifetime_seconds": 600
}
```

Pinning `subject_prefix` to `refs/heads/master` is the whole security
boundary: a wildcard would also match pull-request runs, so anyone who could
open a PR could mint a token against your account. Both workflows check out
`master` regardless of where they were dispatched from, so the pin costs
nothing, except that a manual dispatch launched from a side branch will fail
to authenticate, which is the intended behaviour.

Then add these repo **variables** (Settings → Secrets and variables →
Actions → *Variables*, not Secrets, since they identify the rule rather than
authorize anything):

| Variable | Where to find it |
| --- | --- |
| `ANTHROPIC_FEDERATION_RULE_ID` | `fdrl_…`, the rule created above |
| `ANTHROPIC_ORGANIZATION_ID` | Console → Settings → Organization |
| `ANTHROPIC_SERVICE_ACCOUNT_ID` | `svac_…`, the service account created above |

If both a key secret and the variables exist, the key is used.

## Email

Two repo **secrets**, which stay secrets because Gmail SMTP authenticates
with an App Password:

| Secret | Purpose |
| --- | --- |
| `SMTP_USER` | SMTP username (e.g. your Gmail address) |
| `SMTP_PASS` | SMTP password (Gmail: an [App Password](https://support.google.com/accounts/answer/185833), not your login) |

Optional: `EMAIL_TO` (defaults to `SMTP_USER`, i.e. email yourself),
`SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (default `587`),
`EMAIL_FROM` (default `SMTP_USER`). These default in code and aren't wired
into `daily.yml`; to override one, define the repo secret **and** add its
line back to the "Email the headline" step's `env:` block.

## When it runs

The workflow fires at `23 6 * * *` (06:23 UTC every day, about 08:23 in
Zurich in summer and 07:23 in winter; GitHub cron has no daylight-saving
time; the :23 is deliberate, since GitHub delays or drops runs scheduled on
the congested top of the hour), with two backstop slots later in the day in
case GitHub drops the first. A guard step makes the backstops a one-minute
no-op once a run for the day is on `master`.

**How often it actually searches is `schedule:` in `config/search.yaml`, not
the cron.** A cron line can't read a config file, so the workflow fires every
day and a gate step (`python -m jobradar.schedule --check`) decides whether
today is a run day; a "no" skips the pipeline *and* the email, archive and
commit steps, so a quiet day leaves no trace. With the shipped default
(`daily` on `mon`–`fri`) the effective behaviour is a weekday run.

The cadence is "at least N days since the last recorded run", measured
against `reports/runs.jsonl` rather than a calendar anchor, so a run GitHub
drops doesn't shift the whole schedule; the next day is simply already due.
Preview the decision for any date:

```bash
./.venv/bin/python -m jobradar.schedule --check --date 2026-09-21
```

A manual dispatch from the Actions tab ("Run workflow") is a deliberate act
and always runs, whatever the cadence says; use it to test before trusting the
schedule, with `force_email` ticked to check delivery on a quiet day.

## Monthly discovery

The second workflow, [discover.yml](../.github/workflows/discover.yml),
refreshes the company watch list on the 1st of each month, emails a summary
of newly found companies, and commits the suggestions to
`data/discovered_companies.yaml` for you to review. Same credentials and
secrets. See [DISCOVERY.md](DISCOVERY.md).

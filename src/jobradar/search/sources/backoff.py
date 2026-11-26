"""Ask every site politely: pace requests per site, identify honestly, and
retry when the server says to slow down (429, 503).

Every HTTP client jobradar opens to a job board or a posting page comes from
polite_client(), which puts BackoffTransport under it and sends USER_AGENT.

Pacing. jobradar used to send a board's requests back to back: on the
2026-11-25 run J&J's Workday board got 20 requests in 3 seconds (5.9 a
second) and answered the posting pages after that burst with 429, and Swiss
Life got 105 in 47 seconds. From a home connection a site's own rate limits
and burst-sensitive bot checks are the realistic risk, so requests to one
site are spaced at least MIN_INTERVAL seconds apart, plus up to PACE_JITTER
seconds at random. "One site" is _domain(), so all *.myworkdayjobs.com boards
share one pace. The pace is kept process-wide (_PACER), so a link check
right after a board fetch on the same host waits as well. It covers
onlyfy.jobs's robots.txt Crawl-delay of 1 second too.

Identification. USER_AGENT names jobradar and its version, instead of a
Chrome label on a Python client (a known bot signal) or a bare
"Mozilla/5.0 ... jobradar". It carries no repo URL, which would send the
GitHub username to every site.

Not this: rotating identifiers, imitating a browser more closely, or routing
through other IP addresses. Those get around a site's protection instead of
respecting it.

Retrying. Since 2026-11-16 Workday has answered the GitHub Actions runner's first
request to nearly every board with 429 Too Many Requests, on every datacenter
(wd1 to wd502), while the same requests from a home machine answer 200. A 429
used to cost the whole board at once: the connector raised, the board was
skipped, and the next board's request went out a second later into the same
limit.

BackoffTransport sits under every polite_client and, for a 429
or 503 only, waits and sends the request again: as long as the response's
Retry-After header says, else about 5, 15 and 45 seconds, then gives up and
returns the refusal so the board is skipped as before. Other errors (404, 403)
don't improve with time and pass straight through.

Three things keep a hard block (a refused IP range, which no waiting cures)
from costing a minute per board:
- a Cloudflare bot challenge (a response with `cf-mitigated: challenge`) is
  not retried at all, and the rest of the run sends that domain's requests
  once, unretried. Workday's 429s from 2026-11-16 are this: Cloudflare in
  front of myworkdayjobs.com answers the GitHub Actions runner with a page
  that only a browser running its JavaScript can pass, whatever the pace;
- after two requests on one domain ("myworkdayjobs.com") have used up their
  retries, the rest of the run sends that domain's requests once, unretried;
- a Retry-After longer than MAX_WAIT seconds is not waited out.

The first refusal from each host is logged with its response headers and the
start of its body. That is the evidence for telling a rate limit (Retry-After,
RateLimit-* headers, a JSON error) from a block (a CDN's "Access Denied"
page), which the bare status line can't show. An HTML body is logged as its
visible text: Workday's refusal page opens with several hundred characters of
CSS, so its raw start says nothing about why it refused.
"""

from __future__ import annotations

import html
import logging
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ...updates import code_version

logger = logging.getLogger(__name__)

USER_AGENT = f"jobradar/{code_version() or 'dev'} (job search tool)"
# Requests to one site are at least this many seconds apart...
MIN_INTERVAL = 1.0
# ...plus up to this many more, at random, so the pace isn't a metronome.
PACE_JITTER = 0.5

RETRY_STATUSES = frozenset({429, 503})
# Waits before the 1st, 2nd and 3rd retry when the server names none.
DELAYS = (5.0, 15.0, 45.0)
# A Retry-After longer than this is not waited out: the board is skipped.
MAX_WAIT = 60.0
# Requests on one domain that may use up their retries before that domain's
# requests stop being retried for the rest of the run.
EXHAUSTED_LIMIT = 2
# How much of a refusal's body (an HTML body's visible text) is logged.
BODY_CHARS = 600
_REDACTED_HEADERS = frozenset({"set-cookie", "cookie"})
# What of an HTML page isn't visible text: style and script blocks, comments,
# and the tags themselves.
_HTML_HIDDEN = re.compile(r"<(style|script)\b.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]*>")


def _domain(host: str) -> str:
    """The domain a limit is likely shared across: the host's last two labels
    ("roche.wd3.myworkdayjobs.com" -> "myworkdayjobs.com"). Rough for hosts
    like "example.co.uk", which only makes the give-up rule broader there."""
    return ".".join(host.split(".")[-2:])


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After as seconds to wait: either a number of seconds or an HTTP
    date. None when absent or unreadable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def is_bot_challenge(response: httpx.Response) -> bool:
    """Whether Cloudflare's bot protection answered instead of the site: it
    marks a challenge page with `cf-mitigated: challenge`. Waiting doesn't
    pass one; only a browser that runs the page's JavaScript does."""
    return response.headers.get("cf-mitigated", "").strip().lower() == "challenge"


def visible_text(page: str) -> str:
    """An HTML page's text as a reader would see it: no style, script,
    comments or tags, entities decoded. Rough, but a refusal page is simple."""
    return html.unescape(_HTML_TAG.sub(" ", _HTML_HIDDEN.sub(" ", page)))


def describe_refusal(response: httpx.Response) -> str:
    """The response's headers (cookies redacted) and the start of its body (an
    HTML body's visible text), on one line each, for the log."""
    headers = "; ".join(
        f"{name}: {'<redacted>' if name.lower() in _REDACTED_HEADERS else value}"
        for name, value in response.headers.items()
    )
    try:
        text = response.read().decode("utf-8", "replace")
    except httpx.HTTPError:
        return f"headers: {headers or '(none)'}\n  body: <unreadable>"
    label = "body"
    if "html" in response.headers.get("content-type", "").lower() or text.lstrip().startswith("<"):
        text, label = visible_text(text), "body text"
    body = " ".join(text.split())[:BODY_CHARS]
    return f"headers: {headers or '(none)'}\n  {label}: {body or '(empty)'}"


class HostPacer:
    """Spaces requests to one domain at least `interval` seconds apart (plus
    jitter). Thread-safe: each request reserves its slot under the lock, then
    sleeps outside it, so concurrent callers queue up instead of bursting."""

    def __init__(
        self,
        interval: float = MIN_INTERVAL,
        *,
        jitter: Callable[[], float] = lambda: random.uniform(0, PACE_JITTER),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        """Return once a request to `domain` may go out."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next.get(domain, now))
            self._next[domain] = start + self._interval + self._jitter()
        if start > now:
            self._sleep(start - now)


# Shared by every BackoffTransport in the process, so the pace holds across
# clients (board fetch, link checks, posting fetch) and not only within one.
_PACER = HostPacer()


def polite_client(timeout: float, **kwargs) -> httpx.Client:
    """An httpx client for talking to job boards and posting pages: paced per
    site, retrying 429/503, and sending USER_AGENT. `headers` add to (or
    override) the User-Agent; other keyword arguments go to httpx.Client."""
    headers = {"User-Agent": USER_AGENT, **(kwargs.pop("headers", None) or {})}
    return httpx.Client(timeout=timeout, headers=headers, transport=BackoffTransport(), **kwargs)


class BackoffTransport(httpx.BaseTransport):
    """An httpx transport that paces requests per site and retries 429 and 503
    responses with backoff; see the module docstring. `pacer=None` turns the
    pacing off."""

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        *,
        delays: tuple[float, ...] = DELAYS,
        max_wait: float = MAX_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = lambda delay: random.uniform(0, delay * 0.2),
        pacer: HostPacer | None = _PACER,
    ) -> None:
        self._inner = inner or httpx.HTTPTransport()
        self._pacer = pacer
        self._delays = delays
        self._max_wait = max_wait
        self._sleep = sleep
        self._jitter = jitter
        self._described: set[str] = set()
        self._exhausted: Counter[str] = Counter()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        domain = _domain(host)
        # Read the body once so every retry sends the same bytes.
        request.read()
        attempt = 0
        while True:
            if self._pacer is not None:
                self._pacer.wait(domain)
            response = self._inner.handle_request(request)
            if response.status_code not in RETRY_STATUSES:
                return response
            if host not in self._described:
                self._described.add(host)
                logger.warning(
                    "%s %s from %s; the response:\n  %s",
                    response.status_code, request.method, host, describe_refusal(response),
                )
            if self._exhausted[domain] >= EXHAUSTED_LIMIT:
                return response  # this domain is refusing everything: don't wait
            if is_bot_challenge(response):
                self._exhausted[domain] = EXHAUSTED_LIMIT
                logger.warning(
                    "%s answered with a Cloudflare bot challenge (cf-mitigated: challenge), "
                    "which waiting doesn't pass: not retrying %s for the rest of the run",
                    host, domain,
                )
                return response
            if attempt >= len(self._delays):
                self._give_up(domain, f"still {response.status_code} after {attempt} retries")
                return response
            header = response.headers.get("retry-after")
            wait = retry_after_seconds(header)
            if wait is not None and wait > self._max_wait:
                self._give_up(domain, f"Retry-After {header!r} is longer than {self._max_wait:.0f}s")
                return response
            if wait is None:
                wait = self._delays[attempt] + self._jitter(self._delays[attempt])
            attempt += 1
            logger.info(
                "%s from %s (Retry-After: %s); retry %d of %d in %.0fs",
                response.status_code, host, header or "none", attempt, len(self._delays), wait,
            )
            response.close()
            self._sleep(wait)

    def _give_up(self, domain: str, why: str) -> None:
        self._exhausted[domain] += 1
        if self._exhausted[domain] == EXHAUSTED_LIMIT:
            logger.warning(
                "Giving up on retries for %s for the rest of the run: %s, on %d requests",
                domain, why, EXHAUSTED_LIMIT,
            )
        else:
            logger.warning("Not retrying %s further: %s", domain, why)

    def close(self) -> None:
        self._inner.close()

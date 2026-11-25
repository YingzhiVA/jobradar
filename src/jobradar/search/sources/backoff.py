"""Retry a board's request when the server says to slow down (429, 503).

Since 2026-11-16 Workday has answered the GitHub Actions runner's first
request to nearly every board with 429 Too Many Requests, on every datacenter
(wd1 to wd502), while the same requests from a home machine answer 200. A 429
used to cost the whole board at once: the connector raised, the board was
skipped, and the next board's request went out a second later into the same
limit.

BackoffTransport sits under the board connectors' httpx client and, for a 429
or 503 only, waits and sends the request again: as long as the response's
Retry-After header says, else about 5, 15 and 45 seconds, then gives up and
returns the refusal so the board is skipped as before. Other errors (404, 403)
don't improve with time and pass straight through.

Two things keep a hard block (a refused IP range, which no waiting cures) from
costing a minute per board:
- after two requests on one domain ("myworkdayjobs.com") have used up their
  retries, the rest of the run sends that domain's requests once, unretried;
- a Retry-After longer than MAX_WAIT seconds is not waited out.

The first refusal from each host is logged with its response headers and the
start of its body. That is the evidence for telling a rate limit (Retry-After,
RateLimit-* headers, a JSON error) from a block (a CDN's "Access Denied"
page), which the bare status line can't show.
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 503})
# Waits before the 1st, 2nd and 3rd retry when the server names none.
DELAYS = (5.0, 15.0, 45.0)
# A Retry-After longer than this is not waited out: the board is skipped.
MAX_WAIT = 60.0
# Requests on one domain that may use up their retries before that domain's
# requests stop being retried for the rest of the run.
EXHAUSTED_LIMIT = 2
# How much of a refusal's body is logged.
BODY_CHARS = 300
_REDACTED_HEADERS = frozenset({"set-cookie", "cookie"})


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


def describe_refusal(response: httpx.Response) -> str:
    """The response's headers (cookies redacted) and the start of its body, on
    one line each, for the log."""
    headers = "; ".join(
        f"{name}: {'<redacted>' if name.lower() in _REDACTED_HEADERS else value}"
        for name, value in response.headers.items()
    )
    try:
        body = " ".join(response.read().decode("utf-8", "replace").split())[:BODY_CHARS]
    except httpx.HTTPError:
        body = "<unreadable>"
    return f"headers: {headers or '(none)'}\n  body: {body or '(empty)'}"


class BackoffTransport(httpx.BaseTransport):
    """An httpx transport that retries 429 and 503 responses with backoff; see
    the module docstring."""

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        *,
        delays: tuple[float, ...] = DELAYS,
        max_wait: float = MAX_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = lambda delay: random.uniform(0, delay * 0.2),
    ) -> None:
        self._inner = inner or httpx.HTTPTransport()
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

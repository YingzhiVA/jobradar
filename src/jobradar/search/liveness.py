"""Cheap liveness check for discovered job links.

web_search surfaces leads from the open web, and an LLM reading search snippets
can't reliably tell whether a posting is still open — that's an information
problem no prompt fixes (see sources/web_search.py). The layer that *can*
observe freshness is code that actually fetches the URL, which is what this
module does: drop links that are definitively gone before they reach scoring.

Deliberately conservative — recall-biased to match the "wide net, prune only
clear failures" design:
  * Only a hard "gone" status (404/410) drops a link. A live posting is the
    common case; we don't want to discard a good role over a transient blip.
  * A 403 doesn't drop on its own, but some sites bot-block our browser-y UA
    site-wide and 403 *every* URL — which would hide a real 404 on a closed
    posting. So a 403 triggers one retry with a plainer UA; only if that retry
    reports the posting gone is the link dropped.
  * Network errors / timeouts KEEP the link (benefit of the doubt) rather than
    silently dropping a role because a server was briefly slow.
  * An empty URL is dropped — a lead you can't open is not actionable.

Kept is not the same as checked, so the two are not reported the same way. Every
kept posting carries a `liveness` of LIVENESS_CONFIRMED (we fetched it and it
answered) or LIVENESS_UNVERIFIED (the fetch settled nothing: a 403 that survived
the retry UA, a network error, an exhausted redirect budget), and that state
rides through the pipeline to the report. The recall bias is about what we keep;
it was never a licence to present a guess as a verified link.

The URLs checked here come from LLM web_search output (untrusted, open-web
content), so the fetch is hardened against SSRF: only http(s) URLs whose host
resolves to a public IP are fetched, redirects are followed manually so every
hop is re-checked, and the body is streamed but never read so a hostile
oversized page can't be pulled into memory. A URL that fails the safety check
is dropped (it isn't a real, actionable posting anyway).

Known limitation: a soft-stale aggregator page (e.g. a levels.fyi search URL)
returns 200 even when the specific role closed months ago, so this won't catch
it. That's why web_search.py *also* steers the model away from aggregators
toward direct ATS/employer links, where a closed role actually 404s.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx

from .sources.base import LIVENESS_CONFIRMED, LIVENESS_UNVERIFIED, RawPosting

logger = logging.getLogger(__name__)

# check_url's third verdict, alongside the two LIVENESS_* states a kept posting
# can carry. Not a liveness state because a dead link never reaches a posting —
# it's dropped here.
DEAD = "dead"

_TIMEOUT = 10.0
# Statuses that mean "this URL is gone" — the only signals we drop on.
_DEAD_STATUSES = {404, 410}
# Bound manual redirect-following so a redirect loop can't spin forever.
_MAX_REDIRECTS = 5
# Some career/ATS sites reject the default httpx User-Agent; present a browser-y
# one so a live posting isn't misread as dead.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
# A 403 is ambiguous: some sites bot-block our browser-y UA site-wide and return
# 403 for *every* URL — which masks a real 404/410 on a closed posting. When the
# primary fetch is 403, we take a second look with this plainer UA (which such
# sites often serve normally) and only drop the link if that retry proves it gone.
# The retry is additive evidence only — anything short of a hard "gone" still keeps
# the link, so a genuinely bot-blocked-but-live role isn't discarded.
_RETRY_HEADERS = {"User-Agent": "jobradar-linkcheck/1.0 (+liveness)"}


def _is_public_http_url(url: str) -> bool:
    """SSRF guard: True only for an http(s) URL whose host resolves entirely to
    public IPs. Blocks loopback/private/link-local/reserved targets such as the
    cloud metadata endpoint (169.254.169.254) or internal services.

    Best-effort by design: it validates the IPs the host resolves to now, not
    the socket httpx ultimately connects to, so it does not defend against DNS
    rebinding — adequate here, where the win is blocking obvious internal-address
    fetches from LLM-supplied links.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        return False  # unresolvable host — not reachable, treat as unsafe
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0]).is_global for info in infos
    )


def _probe(url: str, client: httpx.Client, headers: dict | None) -> str:
    """Follow redirects manually and classify the terminal state as one of
    'gone' (404/410), 'unsafe' (fails the SSRF guard at some hop), 'blocked'
    (403 — ambiguous, may be a site-wide UA block), 'inconclusive' (a network
    error or an exhausted redirect budget: the fetch settled nothing either
    way), or 'live' (a status we actually got back and that isn't any of the
    above). The body is streamed and never read, so a hostile oversized page
    can't be pulled into memory.

    'inconclusive' and 'live' are both KEPT downstream — the recall bias is
    unchanged — but they are not the same evidence, so they aren't the same
    verdict: see check_url.

    `headers` overrides the client's default request headers for this probe;
    pass None to use the client's headers (the browser-y UA).
    """
    for _ in range(_MAX_REDIRECTS + 1):
        if not _is_public_http_url(url):
            logger.info("liveness: dropping unsafe/non-public URL %s", url)
            return "unsafe"
        try:
            with client.stream("GET", url, follow_redirects=False, headers=headers) as resp:
                nxt = resp.next_request
                if nxt is None:
                    if resp.status_code in _DEAD_STATUSES:
                        return "gone"
                    return "blocked" if resp.status_code == 403 else "live"
                url = str(nxt.url)
        except httpx.HTTPError as exc:
            # Transient/unknown — keep it rather than drop a role on a network blip.
            logger.info("liveness: keeping %s despite fetch error: %s", url, exc)
            return "inconclusive"
    # Exhausted the redirect budget — keep it (recall bias) rather than drop.
    logger.info("liveness: too many redirects for %s; keeping", url)
    return "inconclusive"


def check_url(url: str, client: httpx.Client) -> str:
    """Classify one URL as DEAD (drop it), LIVENESS_CONFIRMED (fetched, reachable),
    or LIVENESS_UNVERIFIED (fetched, but the fetch proved nothing).

    Redirects are followed manually so each hop is re-checked against the SSRF
    guard; the body is streamed and never read, so a hostile oversized page can't
    be pulled into memory.

    A 403 doesn't drop on its own (recall bias — could be a live role behind bot
    protection), but because some sites 403 our browser-y UA site-wide and thereby
    hide a real 404, a 403 triggers one retry with a plainer UA; only if that retry
    reports the posting gone is the link dropped. When the retry is also blocked,
    the result is UNVERIFIED rather than confirmed-live: the host is refusing to
    answer, and on 2026-08-17 that exact case (swissre.com 403-ing the CI runner's
    IP while serving 410 elsewhere) put two closed roles in the daily report as if
    they were open. The UA retry only defeats a UA-keyed block; an IP-keyed one
    survives it, so the honest answer is "don't know" and the report says so.
    """
    url = (url or "").strip()
    if not url:
        return DEAD
    verdict = _probe(url, client, headers=None)
    if verdict in ("gone", "unsafe"):
        return DEAD
    if verdict == "inconclusive":
        return LIVENESS_UNVERIFIED
    if verdict == "blocked":
        # Our default UA got a 403 — possibly a site-wide block masking a real
        # 404/410. Take a second look with a plainer UA; only a hard 'gone' drops.
        retry = _probe(url, client, headers=_RETRY_HEADERS)
        if retry in ("gone", "unsafe"):
            logger.info(
                "liveness: %s returned 403 to browser UA but is gone under retry UA; dropping",
                url,
            )
            return DEAD
        if retry == "live":
            # A UA-keyed block the plainer UA got past: the host answered, so
            # this one is genuinely settled.
            return LIVENESS_CONFIRMED
        logger.info(
            "liveness: %s did not resolve under the retry UA either (%s); "
            "keeping it as unverified",
            url,
            retry,
        )
        return LIVENESS_UNVERIFIED
    return LIVENESS_CONFIRMED


def is_url_live(url: str, client: httpx.Client) -> bool:
    """True unless the URL is empty, unsafe to fetch, or returns a definitive
    'gone' status — i.e. everything check_url doesn't call DEAD. Use check_url
    directly when the confirmed/unverified distinction matters.
    """
    return check_url(url, client) != DEAD


def filter_live(
    postings: list[RawPosting], *, client: httpx.Client | None = None
) -> tuple[list[RawPosting], list[RawPosting]]:
    """Split postings into (kept, dropped) by checking each URL, stamping each
    kept posting's `liveness` with what the check actually established.

    Only a definitive 'gone' drops (recall bias, unchanged). A kept posting is
    LIVENESS_CONFIRMED or LIVENESS_UNVERIFIED, and that distinction rides along
    to the report — being kept is not the same as being checked.

    Pass a client for testing; otherwise one is created with sane defaults.
    """
    if not postings:
        return [], []
    own_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers=_HEADERS)
    try:
        kept: list[RawPosting] = []
        dropped: list[RawPosting] = []
        for p in postings:
            verdict = check_url(p.url, client)
            if verdict == DEAD:
                dropped.append(p)
                continue
            p.liveness = verdict
            kept.append(p)
        return kept, dropped
    finally:
        if own_client:
            client.close()

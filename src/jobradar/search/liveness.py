"""Cheap liveness check for discovered job links.

web_search surfaces leads from the open web, and an LLM reading search snippets
can't reliably tell whether a posting is still open — that's an information
problem no prompt fixes (see sources/web_search.py). The layer that *can*
observe freshness is code that actually fetches the URL, which is what this
module does: drop links that are definitively gone before they reach scoring.

Deliberately conservative — recall-biased to match the "wide net, prune only
clear failures" design:
  * Only a hard "gone" status (404/410) drops a link, or the redirect an ATS
    sends instead of one: Greenhouse and Workable answer a closed posting by
    redirecting to the company's job list with `?error=true` /
    `?not_found=true`, which then loads fine. A live posting is the common
    case; we don't want to discard a good role over a transient blip.
  * A 403 doesn't drop: it may be a live role behind bot protection. The
    check identifies itself honestly (backoff.USER_AGENT) and doesn't retry
    under another label.
  * Network errors / timeouts KEEP the link (benefit of the doubt) rather than
    silently dropping a role because a server was briefly slow.
  * An empty URL is dropped — a lead you can't open is not actionable.

Kept is not the same as checked, so the two are not reported the same way. Every
kept posting carries a `liveness` of LIVENESS_CONFIRMED (we fetched it and it
answered) or LIVENESS_UNVERIFIED (the fetch settled nothing: a 403, a network
error, an exhausted redirect budget), and that state
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
from urllib.parse import parse_qs, urlsplit

import httpx

from .sources.backoff import polite_client
from .sources.base import LIVENESS_CONFIRMED, LIVENESS_UNVERIFIED, RawPosting

logger = logging.getLogger(__name__)

# check_url's third verdict, alongside the two LIVENESS_* states a kept posting
# can carry. Not a liveness state because a dead link never reaches a posting —
# it's dropped here.
DEAD = "dead"

_TIMEOUT = 10.0
# Statuses that mean "this URL is gone" — the only signals we drop on.
_DEAD_STATUSES = {404, 410}
# Query parameters an ATS adds when it redirects a closed posting to the
# company's job list instead of answering 404: Greenhouse sends
# job-boards.greenhouse.io/<board>?error=true, Workable sends
# apply.workable.com/<account>/?not_found=true. The list itself answers 200, so
# without this a closed posting read as live (2026-11-10: six of nine web_search
# finds, all counted live).
_GONE_REDIRECT_PARAMS = {"error": "true", "not_found": "true"}
# Bound manual redirect-following so a redirect loop can't spin forever.
_MAX_REDIRECTS = 5


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


def _is_gone_redirect(url: str) -> bool:
    """True when a redirect target carries an ATS's "posting not found" marker."""
    query = parse_qs(urlsplit(url).query)
    return any(
        value in query.get(key, ()) for key, value in _GONE_REDIRECT_PARAMS.items()
    )


def _probe(url: str, client: httpx.Client) -> str:
    """Follow redirects manually and classify the terminal state as one of
    'gone' (404/410, or a redirect to an ATS's not-found marker), 'unsafe' (fails the SSRF guard at some hop), 'blocked'
    (403 — ambiguous: a bot block, or a closed posting behind one), 'inconclusive' (a network
    error or an exhausted redirect budget: the fetch settled nothing either
    way), or 'live' (a status we actually got back and that isn't any of the
    above). The body is streamed and never read, so a hostile oversized page
    can't be pulled into memory.

    'inconclusive' and 'live' are both KEPT downstream — the recall bias is
    unchanged — but they are not the same evidence, so they aren't the same
    verdict: see check_url.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        if not _is_public_http_url(url):
            logger.info("liveness: dropping unsafe/non-public URL %s", url)
            return "unsafe"
        try:
            with client.stream("GET", url, follow_redirects=False) as resp:
                nxt = resp.next_request
                if nxt is None:
                    if resp.status_code in _DEAD_STATUSES:
                        return "gone"
                    return "blocked" if resp.status_code == 403 else "live"
                url = str(nxt.url)
                if _is_gone_redirect(url):
                    logger.info("liveness: redirected to a not-found page: %s", url)
                    return "gone"
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

    A 403 doesn't drop (recall bias — could be a live role behind bot
    protection), and it isn't confirmed-live either: the host is refusing to
    answer, and on 2026-08-17 that case (swissre.com 403-ing the CI runner's IP
    while serving 410 elsewhere) put two closed roles in the daily report as if
    they were open. So it is UNVERIFIED, and the report says so.

    A 403 to a Chrome User-Agent used to be retried under a plain
    "jobradar-linkcheck" one, for sites that refuse browser labels from
    non-browsers (datacareer.ch). The check now sends the honest
    backoff.USER_AGENT in the first place, and the only other label left to
    try would be a browser's, which is getting around a block rather than
    respecting it.
    """
    url = (url or "").strip()
    if not url:
        return DEAD
    verdict = _probe(url, client)
    if verdict in ("gone", "unsafe"):
        return DEAD
    if verdict in ("inconclusive", "blocked"):
        if verdict == "blocked":
            logger.info("liveness: %s answered 403; keeping it as unverified", url)
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
    client = client or polite_client(_TIMEOUT)
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

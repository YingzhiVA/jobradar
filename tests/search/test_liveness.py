import httpx

from jobradar.search.liveness import DEAD, _is_public_http_url, check_url, filter_live, is_url_live
from jobradar.search.sources.base import (
    LIVENESS_CONFIRMED,
    LIVENESS_LISTED,
    LIVENESS_UNVERIFIED,
    RawPosting,
)

# Public IP literals are used as test URLs so the real SSRF guard
# (_is_public_http_url) passes without any DNS lookup — getaddrinfo on a literal
# just parses it, so these tests stay offline while still exercising the guard.
_LIVE = "https://8.8.8.8/job/1"
_GONE = "https://8.8.8.8/gone"


class _FakeResponse:
    def __init__(self, status_code, next_url=None):
        self.status_code = status_code
        self.next_request = httpx.Request("GET", next_url) if next_url else None


class _FakeStream:
    """Context manager mimicking httpx.Client.stream(); raises if given one."""

    def __init__(self, result):
        self._result = result

    def __enter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def __exit__(self, *exc_info):
        return False


class _FakeClient:
    """Maps URLs to status codes (or a _FakeResponse, or an Exception to raise).

    A mapped value may also be a dict {"browser": ..., "retry": ...} to return a
    different result depending on the UA: is_url_live passes headers=None for the
    primary (browser-UA) probe and a retry-header dict for the 403 escalation.
    """

    def __init__(self, status_by_url):
        self._status_by_url = status_by_url

    def stream(self, method, url, follow_redirects=False, headers=None):
        result = self._status_by_url.get(url, 200)
        if isinstance(result, dict):
            result = result["retry" if headers else "browser"]
        if isinstance(result, (Exception, _FakeResponse)):
            return _FakeStream(result)
        return _FakeStream(_FakeResponse(result))


def _raw(url):
    return RawPosting(source="web_search", url=url, title="t", company="c", description="d")


def test_live_url_is_kept():
    assert is_url_live(_LIVE, _FakeClient({})) is True


def test_404_is_dropped():
    client = _FakeClient({_GONE: 404})
    assert is_url_live(_GONE, client) is False


def test_410_is_dropped():
    client = _FakeClient({_GONE: 410})
    assert is_url_live(_GONE, client) is False


def test_empty_url_is_dropped():
    assert is_url_live("", _FakeClient({})) is False


def test_other_4xx_is_kept():
    # 401 shouldn't be treated as gone.
    client = _FakeClient({"https://8.8.8.8/unauth": 401})
    assert is_url_live("https://8.8.8.8/unauth", client) is True


def test_403_that_stays_403_on_retry_is_kept():
    # Bot-blocked-but-live: browser UA gets 403, retry UA also 403 (no proof of
    # gone) -> recall bias keeps it.
    url = "https://8.8.8.8/forbidden"
    client = _FakeClient({url: {"browser": 403, "retry": 403}})
    assert is_url_live(url, client) is True


def test_403_that_is_gone_on_retry_is_dropped():
    # The datacareer.ch failure mode: the site 403s our browser UA site-wide but
    # a plainer UA sees the real 404 -> drop. This is the miss this change fixes.
    url = "https://8.8.8.8/stale"
    client = _FakeClient({url: {"browser": 403, "retry": 404}})
    assert is_url_live(url, client) is False


def test_403_that_is_live_on_retry_is_kept():
    # Browser UA blocked (403) but the retry UA gets a normal 200 -> keep.
    url = "https://8.8.8.8/blocked-but-live"
    client = _FakeClient({url: {"browser": 403, "retry": 200}})
    assert is_url_live(url, client) is True


def test_5xx_is_kept():
    client = _FakeClient({"https://8.8.8.8/down": 503})
    assert is_url_live("https://8.8.8.8/down", client) is True


def test_network_error_keeps_link():
    # A transient blip must not drop a possibly-good role.
    client = _FakeClient({"https://8.8.8.8/slow": httpx.ConnectError("boom")})
    assert is_url_live("https://8.8.8.8/slow", client) is True


def test_redirect_is_followed_to_final_status():
    # A redirect chain ending in 404 is treated as gone.
    client = _FakeClient(
        {
            _LIVE: _FakeResponse(301, next_url=_GONE),
            _GONE: _FakeResponse(404),
        }
    )
    assert is_url_live(_LIVE, client) is False


def test_redirect_loop_is_bounded_and_kept():
    # A self-redirect must not spin forever; recall bias keeps it.
    looping = "https://8.8.8.8/loop"
    client = _FakeClient({looping: _FakeResponse(302, next_url=looping)})
    assert is_url_live(looping, client) is True


def test_non_http_scheme_is_dropped():
    assert _is_public_http_url("file:///etc/passwd") is False
    assert _is_public_http_url("ftp://8.8.8.8/x") is False


def test_internal_and_metadata_ips_are_dropped():
    # SSRF targets: loopback, private ranges, and the cloud metadata endpoint.
    assert _is_public_http_url("http://127.0.0.1/") is False
    assert _is_public_http_url("http://10.0.0.5/") is False
    assert _is_public_http_url("http://192.168.1.1/") is False
    assert _is_public_http_url("http://169.254.169.254/latest/meta-data/") is False
    assert _is_public_http_url("http://[::1]/") is False


def test_public_ip_is_allowed():
    assert _is_public_http_url("https://8.8.8.8/job/1") is True


def test_unsafe_url_is_not_fetched():
    # An internal URL is dropped without ever calling the client.
    class _Boom:
        def stream(self, *a, **k):
            raise AssertionError("must not fetch an unsafe URL")

    assert is_url_live("http://169.254.169.254/", _Boom()) is False


def test_filter_live_splits_live_and_dead():
    client = _FakeClient(
        {
            "https://8.8.8.8/live": 200,
            "https://8.8.8.8/dead": 404,
        }
    )
    live, dropped = filter_live(
        [_raw("https://8.8.8.8/live"), _raw("https://8.8.8.8/dead")],
        client=client,
    )
    assert [p.url for p in live] == ["https://8.8.8.8/live"]
    assert [p.url for p in dropped] == ["https://8.8.8.8/dead"]


def test_filter_live_empty_input():
    assert filter_live([]) == ([], [])


# --- Kept-vs-checked: what a kept posting's liveness state says ---------------


def test_reachable_url_is_confirmed():
    assert check_url(_LIVE, _FakeClient({})) == LIVENESS_CONFIRMED


def test_gone_url_is_dead():
    assert check_url(_GONE, _FakeClient({_GONE: 410})) == DEAD


def test_persistent_403_is_unverified_not_confirmed():
    # The 2026-08-17 Swiss Re failure: swissre.com 403'd the CI runner's IP under
    # both UAs while serving 410 elsewhere. Recall bias still KEEPS the link (the
    # host may just be bot-blocking a live role) but it must not be reported as
    # verified — that is what let two closed roles reach the daily report.
    url = "https://8.8.8.8/forbidden"
    client = _FakeClient({url: {"browser": 403, "retry": 403}})
    assert is_url_live(url, client) is True
    assert check_url(url, client) == LIVENESS_UNVERIFIED


def test_403_rescued_by_retry_ua_is_confirmed():
    # A UA-keyed block that the plain-UA retry gets past is genuinely resolved.
    url = "https://8.8.8.8/blocked-but-live"
    client = _FakeClient({url: {"browser": 403, "retry": 200}})
    assert check_url(url, client) == LIVENESS_CONFIRMED


def test_network_error_is_unverified():
    client = _FakeClient({"https://8.8.8.8/slow": httpx.ConnectError("boom")})
    assert check_url("https://8.8.8.8/slow", client) == LIVENESS_UNVERIFIED


def test_redirect_loop_is_unverified():
    looping = "https://8.8.8.8/loop"
    client = _FakeClient({looping: _FakeResponse(302, next_url=looping)})
    assert check_url(looping, client) == LIVENESS_UNVERIFIED


def test_5xx_is_confirmed_reachable():
    # A 503 is the host answering — it isn't the "we learned nothing" case.
    client = _FakeClient({"https://8.8.8.8/down": 503})
    assert check_url("https://8.8.8.8/down", client) == LIVENESS_CONFIRMED


def test_filter_live_stamps_liveness_on_kept_postings():
    blocked = "https://8.8.8.8/blocked"
    client = _FakeClient(
        {
            "https://8.8.8.8/live": 200,
            blocked: {"browser": 403, "retry": 403},
            "https://8.8.8.8/dead": 404,
        }
    )
    kept, dropped = filter_live(
        [_raw("https://8.8.8.8/live"), _raw(blocked), _raw("https://8.8.8.8/dead")],
        client=client,
    )
    assert [(p.url, p.liveness) for p in kept] == [
        ("https://8.8.8.8/live", LIVENESS_CONFIRMED),
        (blocked, LIVENESS_UNVERIFIED),
    ]
    assert [p.url for p in dropped] == ["https://8.8.8.8/dead"]


def test_unprobed_posting_defaults_to_listed():
    # A connector that never probes (an ATS listing its own roles) is not suspect.
    assert _raw(_LIVE).liveness == LIVENESS_LISTED

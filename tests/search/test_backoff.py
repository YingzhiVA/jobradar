import logging
from datetime import datetime, timezone

import httpx
import pytest

from jobradar.search.sources.backoff import (
    USER_AGENT,
    BackoffTransport,
    HostPacer,
    is_bot_challenge,
    polite_client,
    retry_after_seconds,
)


def _transport(responses, **kwargs):
    """A BackoffTransport over a mock that answers from `responses` in order,
    recording each request and each sleep instead of sleeping."""
    seen, slept = [], []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        status, headers, body = queue.pop(0)
        return httpx.Response(status, headers=headers, content=body)

    transport = BackoffTransport(
        httpx.MockTransport(handler), sleep=slept.append, jitter=lambda delay: 0.0, pacer=None, **kwargs
    )
    return transport, seen, slept


def test_a_429_is_retried_after_the_backoff_delay_and_the_body_resent():
    transport, seen, slept = _transport([(429, {}, b""), (200, {}, b'{"ok": 1}')])
    with httpx.Client(transport=transport) as client:
        response = client.post("https://acme.wd3.myworkdayjobs.com/jobs", json={"limit": 1})
    assert response.status_code == 200 and response.json() == {"ok": 1}
    assert slept == [5.0]
    assert [r.content for r in seen] == [b'{"limit":1}', b'{"limit":1}']


def test_retry_after_seconds_is_honoured():
    transport, _, slept = _transport([(503, {"Retry-After": "2"}, b""), (200, {}, b"")])
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.com/").status_code == 200
    assert slept == [2.0]


def test_after_three_retries_the_refusal_is_returned_and_the_board_skipped_as_before():
    transport, seen, slept = _transport([(429, {}, b"")] * 4)
    with httpx.Client(transport=transport) as client:
        response = client.get("https://acme.wd3.myworkdayjobs.com/jobs")
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()
    assert slept == [5.0, 15.0, 45.0] and len(seen) == 4


def test_a_domain_that_used_up_its_retries_twice_is_not_retried_again():
    transport, seen, slept = _transport([(429, {}, b"")] * 9)
    with httpx.Client(transport=transport) as client:
        client.get("https://a.wd3.myworkdayjobs.com/jobs")
        client.get("https://b.wd5.myworkdayjobs.com/jobs")
        assert len(slept) == 6
        # A third board on the same domain: sent once, no waiting.
        assert client.get("https://c.wd1.myworkdayjobs.com/jobs").status_code == 429
    assert len(slept) == 6 and len(seen) == 9


def test_a_retry_after_longer_than_the_cap_is_not_waited_out():
    transport, seen, slept = _transport([(429, {"Retry-After": "3600"}, b"")])
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.com/").status_code == 429
    assert slept == [] and len(seen) == 1


def test_other_errors_pass_straight_through():
    transport, seen, slept = _transport([(404, {}, b"gone")])
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.com/").status_code == 404
    assert slept == [] and len(seen) == 1


def test_the_first_refusal_per_host_is_logged_with_headers_and_body(caplog):
    refusal = (429, {"Retry-After": "1", "X-RateLimit-Remaining": "0", "Set-Cookie": "session=secret"}, b"<html>  Access   Denied </html>")
    transport, _, _ = _transport([refusal, refusal, (200, {}, b"")])
    with caplog.at_level(logging.INFO, logger="jobradar.search.sources.backoff"):
        with httpx.Client(transport=transport) as client:
            client.get("https://acme.wd3.myworkdayjobs.com/jobs")
    described = [r.getMessage() for r in caplog.records if "the response:" in r.getMessage()]
    assert len(described) == 1  # once per host, not per retry
    assert "retry-after: 1" in described[0] and "x-ratelimit-remaining: 0" in described[0]
    assert "body text: Access Denied" in described[0]
    assert "secret" not in described[0] and "set-cookie: <redacted>" in described[0]


def test_an_html_refusal_is_logged_as_its_visible_text(caplog):
    page = (
        b"<!DOCTYPE html><html><head><style>body { text-align: center; }"
        + b" .wdayLogo { padding: 16px; }" * 40
        + b"</style><script>var x = '<b>no</b>';</script><title>Workday</title></head>"
        b"<body><!-- tracking --><div class='graphicsContainer'></div>"
        b"<p>Your request was blocked &amp; logged.</p></body></html>"
    )
    refusal = (429, {"Content-Type": "text/html; charset=UTF-8"}, page)
    transport, _, _ = _transport([refusal, (200, {}, b"")])
    with caplog.at_level(logging.WARNING, logger="jobradar.search.sources.backoff"):
        with httpx.Client(transport=transport) as client:
            client.post("https://acme.wd3.myworkdayjobs.com/jobs", json={})
    described = next(r.getMessage() for r in caplog.records if "the response:" in r.getMessage())
    assert "body text: Workday Your request was blocked & logged." in described
    assert "wdayLogo" not in described and "var x" not in described and "tracking" not in described


def test_a_json_refusal_is_logged_as_is(caplog):
    refusal = (429, {"Content-Type": "application/json"}, b'{"error": "rate <limit>"}')
    transport, _, _ = _transport([refusal, (200, {}, b"")])
    with caplog.at_level(logging.WARNING, logger="jobradar.search.sources.backoff"):
        with httpx.Client(transport=transport) as client:
            client.get("https://api.example.com/jobs")
    described = next(r.getMessage() for r in caplog.records if "the response:" in r.getMessage())
    assert 'body: {"error": "rate <limit>"}' in described


def test_retry_after_parses_seconds_and_dates():
    now = datetime(2026, 11, 25, 17, 0, 0, tzinfo=timezone.utc)
    assert retry_after_seconds("30") == 30.0
    assert retry_after_seconds("Wed, 25 Nov 2026 17:00:20 GMT", now=now) == 20.0
    assert retry_after_seconds("Wed, 25 Nov 2026 16:00:00 GMT", now=now) == 0.0
    assert retry_after_seconds(None) is None and retry_after_seconds("soon") is None


def test_a_cloudflare_challenge_is_not_retried_and_stops_retries_on_its_domain(caplog):
    challenge = (429, {"cf-mitigated": "challenge", "Content-Type": "text/html"}, b"<title>Just a moment...</title>")
    plain = (429, {}, b"")
    transport, seen, slept = _transport([challenge, plain, plain, (200, {}, b"")])
    with caplog.at_level(logging.WARNING, logger="jobradar.search.sources.backoff"):
        with httpx.Client(transport=transport) as client:
            assert client.post("https://nvidia.wd5.myworkdayjobs.com/jobs", json={}).status_code == 429
            # Another board on the same domain: sent once, not retried.
            assert client.post("https://roche.wd3.myworkdayjobs.com/jobs", json={}).status_code == 429
            # A different domain still gets its retries.
            assert client.get("https://api.example.com/jobs").status_code == 200
    assert slept == [5.0]  # only example.com's one retry
    assert len(seen) == 4
    assert any("Cloudflare bot challenge" in r.getMessage() for r in caplog.records)


def test_is_bot_challenge_reads_the_cf_mitigated_header():
    assert is_bot_challenge(httpx.Response(429, headers={"cf-mitigated": "challenge"}))
    assert is_bot_challenge(httpx.Response(403, headers={"CF-Mitigated": " Challenge "}))
    assert not is_bot_challenge(httpx.Response(429, headers={"server": "cloudflare"}))
    assert not is_bot_challenge(httpx.Response(429))


class _Clock:
    """A fake monotonic clock that sleeping advances."""

    def __init__(self):
        self.now = 100.0
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(round(seconds, 6))
        self.now += seconds


def _pacer(clock, interval=1.0):
    return HostPacer(interval, jitter=lambda: 0.0, clock=lambda: clock.now, sleep=clock.sleep)


def test_requests_to_one_site_are_spaced_but_the_first_goes_at_once():
    clock = _Clock()
    pacer = _pacer(clock)
    for _ in range(3):
        pacer.wait("myworkdayjobs.com")
    assert clock.slept == [1.0, 1.0]


def test_the_pace_is_per_site():
    clock = _Clock()
    pacer = _pacer(clock)
    pacer.wait("myworkdayjobs.com")
    pacer.wait("greenhouse.io")
    assert clock.slept == []


def test_a_request_after_the_interval_has_passed_does_not_wait():
    clock = _Clock()
    pacer = _pacer(clock)
    pacer.wait("swisslife.ch")
    clock.now += 3.0  # a slow response
    pacer.wait("swisslife.ch")
    assert clock.slept == []


def test_the_transport_paces_boards_on_one_domain_together():
    # roche.wd3 and jj.wd5 share myworkdayjobs.com, so they share one pace.
    clock = _Clock()
    transport = BackoffTransport(httpx.MockTransport(lambda r: httpx.Response(200)), pacer=_pacer(clock))
    with httpx.Client(transport=transport) as client:
        client.get("https://roche.wd3.myworkdayjobs.com/jobs")
        client.get("https://jj.wd5.myworkdayjobs.com/jobs")
        client.get("https://boards-api.greenhouse.io/v1/boards/x/jobs")
    assert clock.slept == [1.0]


def test_polite_client_identifies_as_jobradar_not_a_browser():
    assert USER_AGENT.startswith("jobradar/") and "Mozilla" not in USER_AGENT
    with polite_client(5.0, headers={"Accept": "text/html"}) as client:
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.headers["Accept"] == "text/html"

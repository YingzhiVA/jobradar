import logging
from datetime import datetime, timezone

import httpx
import pytest

from jobradar.search.sources.backoff import BackoffTransport, retry_after_seconds


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
        httpx.MockTransport(handler), sleep=slept.append, jitter=lambda delay: 0.0, **kwargs
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
    assert "<html> Access Denied </html>" in described[0]
    assert "secret" not in described[0] and "set-cookie: <redacted>" in described[0]


def test_retry_after_parses_seconds_and_dates():
    now = datetime(2026, 11, 25, 17, 0, 0, tzinfo=timezone.utc)
    assert retry_after_seconds("30") == 30.0
    assert retry_after_seconds("Wed, 25 Nov 2026 17:00:20 GMT", now=now) == 20.0
    assert retry_after_seconds("Wed, 25 Nov 2026 16:00:00 GMT", now=now) == 0.0
    assert retry_after_seconds(None) is None and retry_after_seconds("soon") is None

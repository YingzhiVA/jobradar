import anthropic
import httpx
import pytest

from jobradar.search.main import AuthenticationConfigError, _check_authentication


class _FakeModels:
    def __init__(self, exc: Exception | None = None):
        self._exc = exc

    def list(self, limit: int | None = None):
        if self._exc is not None:
            raise self._exc
        return []


class _FakeClient:
    def __init__(self, exc: Exception | None = None):
        self.models = _FakeModels(exc)


def _make_authentication_error() -> anthropic.AuthenticationError:
    response = httpx.Response(status_code=401, request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))
    return anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)


def test_check_authentication_passes_when_models_list_succeeds():
    _check_authentication(_FakeClient())  # should not raise


def test_check_authentication_raises_on_missing_credentials():
    # This is the failure mode the SDK actually produces when no credential
    # source resolves at all (see main.py's comment) — a client-side
    # TypeError, not a server-returned AuthenticationError.
    client = _FakeClient(TypeError("Could not resolve authentication method"))
    with pytest.raises(AuthenticationConfigError, match="No Anthropic credentials configured"):
        _check_authentication(client)


def test_check_authentication_raises_on_invalid_credentials():
    client = _FakeClient(_make_authentication_error())
    with pytest.raises(AuthenticationConfigError, match="authentication failed"):
        _check_authentication(client)


def test_check_authentication_does_not_swallow_unrelated_errors():
    client = _FakeClient(ValueError("something unrelated went wrong"))
    with pytest.raises(ValueError, match="something unrelated"):
        _check_authentication(client)

"""A stand-in for `client.messages.stream(...)`.

The apply pipeline's model calls all stream now, so a fake client has to look
like the SDK's stream helper: a context manager exposing `text_stream` (the
text deltas, which is what the progress counter reads) and
`get_final_message()` (the accumulated response, which is what the caller
actually uses). Anything that only drains one of the two would pass against a
fake and hang or return None against the real SDK.
"""

from __future__ import annotations

import pytest


class FakeStream:
    def __init__(self, chunks: list[str], message) -> None:
        self.text_stream = iter(chunks)
        self._message = message
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeStream":
        self.entered = True
        return self

    def __exit__(self, *exc) -> None:
        self.exited = True

    def get_final_message(self):
        return self._message


class FakeMessage:
    """The fields the pipeline reads off a finished response."""

    def __init__(self, parsed_output, stop_reason: str = "end_turn", usage=None) -> None:
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = usage


class FakeUsage:
    def __init__(
        self,
        input_tokens: int = 10,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeMessages:
    def __init__(self, chunks: list[str], message) -> None:
        self._chunks = chunks
        self._message = message
        self.kwargs: dict = {}
        self.stream_obj: FakeStream | None = None

    def stream(self, **kwargs) -> FakeStream:
        self.kwargs = kwargs
        self.stream_obj = FakeStream(list(self._chunks), self._message)
        return self.stream_obj


class FakeClient:
    def __init__(self, chunks: list[str], parsed_output, **message_kwargs) -> None:
        self.messages = FakeMessages(
            chunks, FakeMessage(parsed_output, **message_kwargs)
        )


@pytest.fixture
def fake_client():
    return FakeClient

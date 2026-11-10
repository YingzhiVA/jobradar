from jobradar.models import Posting, ScoredPosting
from jobradar.search.ranking import RankedPosting
from jobradar.search.writeup import _TRUNCATION_NOTE, _WRITEUP_ATTEMPTS, write_rationale


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Text(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.usage = None


class _Messages:
    """Replays a script of outcomes, one per call: a _Response, or an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0
        self.kwargs = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        outcome = self.script[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, *script):
        self.messages = _Messages(*script)


def _ranked():
    posting = Posting(
        id="p", source="test", url="https://example.invalid/p",
        title="PM", company="Co", description="d",
    )
    scored = ScoredPosting(
        posting=posting, skill_score=80, interest_score=70,
        best_cv="pm", brief_reason="the brief reason",
    )
    return RankedPosting(scored=scored, tier="okay")


def test_a_complete_write_up_is_used_as_is():
    client = _Client(_Response("Full write-up."))
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == "Full write-up."
    assert client.messages.calls == 1


def test_a_truncated_write_up_is_retried():
    """The case that shipped a sentence ending "that is exactly the " into a
    report: a second draw usually comes back shorter.
    """
    client = _Client(
        _Response("Cut off mid sen", stop_reason="max_tokens"),
        _Response("Complete second draw."),
    )
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == "Complete second draw."
    assert client.messages.calls == 2


def test_truncation_on_every_attempt_is_said_in_the_report():
    """A warning in the run log went unnoticed; the report is where the reader
    looks, so an unfinished write-up has to say it is unfinished there.
    """
    client = _Client(*[
        _Response("Cut off mid sen", stop_reason="max_tokens")
        for _ in range(_WRITEUP_ATTEMPTS)
    ])
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == "Cut off mid sen" + _TRUNCATION_NOTE


def test_an_api_error_is_retried():
    client = _Client(RuntimeError("500"), _Response("Recovered."))
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == "Recovered."


def test_every_attempt_failing_falls_back_to_the_brief_reason():
    client = _Client(*[RuntimeError("500") for _ in range(_WRITEUP_ATTEMPTS)])
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == "the brief reason"


def test_a_truncated_draw_is_kept_if_the_retry_errors():
    """Better an unfinished write-up, marked as such, than the one-line brief
    reason — the truncated text is still most of the analysis.
    """
    client = _Client(
        _Response("Most of the analysis", stop_reason="max_tokens"),
        RuntimeError("500"),
    )
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == "Most of the analysis" + _TRUNCATION_NOTE


def test_a_quotation_in_the_write_up_survives():
    # As a JSON string field, the write-up ended at the model's first unescaped
    # quote; plain text has nothing for a quote to close.
    text = 'Your C# work satisfies the "strong engineering foundation" ask.\n\nNext paragraph.'
    client = _Client(_Response(text))
    final = write_rationale(_ranked(), "profile", client)
    assert final.writeup == text
    assert "output_format" not in client.messages.kwargs[0]


def test_an_empty_reply_is_retried():
    client = _Client(_Response("   "), _Response("The write-up."))
    assert write_rationale(_ranked(), "profile", client).writeup == "The write-up."


def test_headings_are_demoted_so_the_report_keeps_its_structure():
    # A "## " line would end the match block when apply/findings.py reads the
    # report back.
    client = _Client(_Response("## Manager, Acme\n\n### Why it fits\n\nIt fits.\n\n#hashtag stays"))
    assert write_rationale(_ranked(), "profile", client).writeup == (
        "**Manager, Acme**\n\n**Why it fits**\n\nIt fits.\n\n#hashtag stays"
    )


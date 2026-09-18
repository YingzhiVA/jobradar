"""The transparency contract: a long run has to keep saying what it is doing.

The failure this guards against is not a wrong answer, it is silence. A
drafting run makes three to five model calls per posting and the writing ones
take minutes; before this, the terminal showed one httpx line and then
nothing, which is indistinguishable from a hang. So what is pinned here is
that every slow step announces itself, keeps a heartbeat while it runs, and
reports how long it took - and, on the model calls, that the streamed text
actually reaches the counter that proves the wait is productive.
"""

from __future__ import annotations

import logging

import pytest

from jobradar.apply import extract, prep, progress, tailor
from jobradar.apply.extract import JDExtract
from jobradar.apply.prep import PrepSheet
from jobradar.apply.tailor import ApplicationDocs, TranslatedCV
from jobradar.apply.util import drain_stream

from .conftest import FakeStream


# --- formatting -------------------------------------------------------------


def test_fmt_duration_scales_with_length():
    assert progress.fmt_duration(0) == "0s"
    assert progress.fmt_duration(4.4) == "4s"
    assert progress.fmt_duration(59.6) == "60s"
    assert progress.fmt_duration(132) == "2m12s"
    assert progress.fmt_duration(3782) == "1h03m"


def test_fmt_duration_never_shows_negative_time():
    # A clock that reads backwards is a bug report, not a progress line.
    assert progress.fmt_duration(-1) == "0s"


def test_fmt_count_switches_to_thousands():
    assert progress.fmt_count(0) == "0"
    assert progress.fmt_count(999) == "999"
    assert progress.fmt_count(11_900) == "11.9k"


# --- the stage counter ------------------------------------------------------


def test_stage_counts_the_streamed_text():
    stage = progress.Stage("writing")
    stage.advance("hello")
    stage.advance(" world")
    assert stage.chars == 11
    assert "11 chars" in stage.summary()


def test_stage_summary_omits_the_counter_before_any_text():
    # A page fetch streams nothing back; "(2s, 0 chars)" would read as a
    # failure rather than as a step that has no text to report.
    stage = progress.Stage("fetching")
    assert "chars" not in stage.summary()


# --- the live terminal line -------------------------------------------------


def test_live_line_keeps_the_counters_when_the_label_will_not_fit():
    # A narrow terminal has to sacrifice something. It must not be the
    # counters: they are what tells "working" from "wedged".
    line = progress._fit(
        "* ",
        "[1/2] writing the tailored CV + cover letter for AXA Schweiz (Sonnet)",
        "  2s, 10.8k chars",
        79,
    )
    assert len(line) <= 79
    assert line.endswith("2s, 10.8k chars")
    assert line.startswith("* [1/2] writing the tailored")
    assert "..." in line  # the middle of the label is what gave way


def test_live_line_is_left_alone_when_it_fits():
    line = progress._fit("* ", "[1/2] fetching the posting page", "  1s", 79)
    assert line == "* [1/2] fetching the posting page  1s"


def test_live_line_never_wraps_even_on_a_useless_width():
    # Wrapping would scroll the terminal and defeat the in-place repaint, so a
    # hopeless width gets a flat cut rather than a multi-line spinner.
    line = progress._fit("* ", "writing the tailored CV", "  2s, 10.8k chars", 12)
    assert len(line) <= 12


# --- what reaches the log ---------------------------------------------------


def test_step_logs_the_label_and_the_elapsed_time(caplog):
    with caplog.at_level(logging.INFO, logger="jobradar.apply.progress"):
        with progress.step("reading the job description") as stage:
            stage.advance("x" * 1500)
    done = caplog.records[-1].getMessage()
    assert done.startswith("finished: reading the job description")
    assert "1.5k chars" in done


def test_step_reports_a_failure_instead_of_swallowing_it(caplog):
    with caplog.at_level(logging.INFO, logger="jobradar.apply.progress"):
        with pytest.raises(ValueError):
            with progress.step("writing the tailored CV"):
                raise ValueError("boom")
    # The exception still propagates - the step only narrates.
    assert caplog.records[-1].getMessage().startswith("failed: writing the tailored CV")


def test_heartbeat_says_the_step_is_still_running(caplog, monkeypatch):
    # Non-interactive output (a CI log, a cron mail) gets log lines rather
    # than a repainted line, and this is the one that proves a slow step is
    # alive rather than stuck.
    monkeypatch.setattr(progress, "_is_tty", lambda: False)
    monkeypatch.setattr(progress, "HEARTBEAT_SECONDS", 0.01)
    with caplog.at_level(logging.INFO, logger="jobradar.apply.progress"):
        with progress.step("translating the CV to German") as stage:
            stage.advance("Sehr geehrte Damen und Herren")
            _wait_for(lambda: any("still running" in r.getMessage() for r in caplog.records))
    beat = next(r.getMessage() for r in caplog.records if "still running" in r.getMessage())
    assert "translating the CV to German" in beat
    assert "29 chars" in beat


def test_progress_can_be_switched_off_and_still_names_the_step(caplog, monkeypatch):
    monkeypatch.setenv("JOBRADAR_PROGRESS", "0")
    with caplog.at_level(logging.INFO, logger="jobradar.apply.progress"):
        with progress.step("rendering the PDFs"):
            pass
    assert not progress.enabled()
    # No ticker, but the step is still named at both ends: turning the
    # indicator off must not take the timeline with it.
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("starting: rendering the PDFs") for m in messages)
    assert messages[-1].startswith("finished: rendering the PDFs")


def _wait_for(predicate, timeout: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


# --- streaming reaches the counter ------------------------------------------


def test_drain_stream_forwards_every_delta():
    seen: list[str] = []
    stream = FakeStream(["ab", "cd"], object())
    drain_stream(stream, seen.append)
    assert seen == ["ab", "cd"]


def test_drain_stream_consumes_the_stream_without_a_callback():
    # get_final_message() blocks until the stream is read, so draining is not
    # optional just because nobody is watching the deltas.
    stream = FakeStream(["ab", "cd"], object())
    drain_stream(stream)
    assert list(stream.text_stream) == []


def test_extract_jd_streams_into_the_progress_counter(fake_client):
    stage = progress.Stage("reading")
    client = fake_client(
        ['{"is_job_posting":', ' true, "title": "PM"}'],
        JDExtract(is_job_posting=True, title="PM"),
    )
    jd = extract.extract_jd("page text", client, on_delta=stage.advance)
    assert jd.title == "PM"
    assert stage.chars == 39
    assert client.messages.stream_obj.exited  # the stream is closed, not leaked


def test_write_application_docs_streams_into_the_progress_counter(fake_client):
    stage = progress.Stage("writing")
    client = fake_client(["# CV", "\n- bullet"], ApplicationDocs(tailored_cv_markdown="# CV"))
    docs = tailor.write_application_docs(
        "CV_X", "en", False, "profile", "jd", client, on_delta=stage.advance
    )
    assert docs.tailored_cv_markdown == "# CV"
    assert stage.chars == 13


def test_write_application_docs_reports_unparseable_output_instead_of_crashing(fake_client):
    # A truncated response parses to nothing; the caller wants None (leave the
    # entry queued for a re-run), not an AttributeError mid-batch.
    client = fake_client(["{ truncated"], None)
    assert tailor.write_application_docs(
        "CV_X", "en", False, "profile", "jd", client
    ) is None


def test_translate_cv_streams_into_the_progress_counter(fake_client):
    stage = progress.Stage("translating")
    client = fake_client(
        ["# Lebens", "lauf"], TranslatedCV(translated_cv_markdown="# Lebenslauf")
    )
    out = tailor.translate_cv("# CV", "de", "profile", "jd", client, on_delta=stage.advance)
    assert out.strip() == "# Lebenslauf"
    assert stage.chars == 12


def test_generate_prep_streams_into_the_progress_counter(fake_client):
    stage = progress.Stage("prepping")
    client = fake_client(['{"positioning"', ': "I ship."}'], PrepSheet(positioning="I ship."))
    sheet = prep.generate_prep("profile", "jd", client, on_delta=stage.advance)
    assert sheet.positioning == "I ship."
    assert stage.chars == 26


def test_a_failed_call_still_returns_none(fake_client):
    class _Boom:
        def stream(self, **kwargs):
            raise RuntimeError("connection reset")

    class _Client:
        messages = _Boom()

    assert extract.extract_jd("page", _Client()) is None
    assert tailor.write_application_docs("CV_X", "en", False, "p", "j", _Client()) is None
    assert tailor.translate_cv("# CV", "de", "p", "j", _Client()) is None
    assert prep.generate_prep("p", "j", _Client()) is None


# --- the live line must not eat log records ---------------------------------


def test_install_wraps_handlers_once(monkeypatch):
    monkeypatch.setattr(progress, "_is_tty", lambda: True)
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        progress.install()
        first = handler.emit
        progress.install()
        # Wrapping a wrapped handler would clear the line twice per record and
        # grow a new closure on every call.
        assert handler.emit is first
        assert hasattr(handler.emit, "_jobradar_wrapped")
    finally:
        root.removeHandler(handler)


def test_a_log_record_wipes_the_live_line_before_it_is_written(monkeypatch):
    # The record and the wipe have to arrive in that order and with nothing
    # in between, or the spinner's next repaint erases the record.
    monkeypatch.setattr(progress, "_is_tty", lambda: True)
    written: list[str] = []
    monkeypatch.setattr(progress, "_write", written.append)

    handler = logging.StreamHandler()
    handler.emit = lambda record: written.append(f"LOG:{record.getMessage()}")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        progress.install()
        progress._paint("* ", "writing", "  2s")
        logging.getLogger("jobradar.apply.progress").error("something went wrong")
    finally:
        root.removeHandler(handler)

    assert written[-2].startswith("\r")  # the wipe
    assert written[-1] == "LOG:something went wrong"
    assert not progress._line_painted

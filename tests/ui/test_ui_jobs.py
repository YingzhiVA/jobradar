import sys
import time

import pytest

from jobradar.ui import jobs
from jobradar.ui.setup import SetupError


def test_every_action_builds_a_command_for_this_interpreter():
    params = {"uid": "7", "url": "https://jobs.example.ch/1", "month": "2026-11"}
    for name, action in jobs.ACTIONS.items():
        command = jobs._command(action, params)
        assert command[:3] == [sys.executable, "-m", action.module], name
        assert all(isinstance(part, str) for part in command), name


def test_draft_passes_the_url_as_one_argument():
    url = "https://jobs.example.ch/1?a=b&c=d;rm"
    assert jobs._command(jobs.ACTIONS["draft"], {"url": f"  {url} ", "full": True})[3:] == ["--url", url, "--full"]


@pytest.mark.parametrize("action, params", [
    ("draft", {"url": "file:///etc/passwd"}),
    ("draft", {"url": "--commit"}),
    ("draft", {"url": "https://a.example/x --commit"}),
    ("draft", {}),
    ("prep", {"uid": "7 --force"}),
    ("pdf", {"uid": "--lint"}),
    ("submitted", {"uid": "../x"}),
    ("rav", {"month": "2026-11 --commit"}),
    ("rav_filed", {"month": "2026-11 --commit"}),
    ("rav_filed", {}),
])
def test_values_that_could_read_as_another_argument_are_refused(action, params):
    with pytest.raises(SetupError):
        jobs._command(jobs.ACTIONS[action], params)


@pytest.mark.parametrize("line, shown", [
    ("12:49:01 INFO jobradar.search.main: 133/4951 postings are new\n", "133/4951 postings are new"),
    ("WARNING jobradar.apply.main: needs a pasted JD", "needs a pasted JD"),
    ("| 2026-11-10 | Acme |", "| 2026-11-10 | Acme |"),
])
def test_log_prefix_is_dropped_for_display(line, shown):
    assert jobs.clean(line) == shown


def _wait(job: jobs.Job) -> str:
    deadline = time.time() + 20
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.05)
    return job.state


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """A runner whose two actions are small Python programs, not jobradar."""
    monkeypatch.setitem(jobs.ACTIONS, "hello", jobs.Action(
        "Saying hello to #{uid}", "this-is-replaced", lambda p: [],
    ))
    programs = {
        "hello": "import sys; print('INFO x.y: one'); print('two', file=sys.stderr); sys.exit(3)",
        "sleep": "import time; print('started', flush=True); time.sleep(60)",
    }
    monkeypatch.setitem(jobs.ACTIONS, "sleep", jobs.Action("Sleeping", "this-is-replaced", lambda p: []))
    monkeypatch.setattr(jobs, "_command", lambda action, params: [
        sys.executable, "-c", programs["hello" if action.label.startswith("Saying") else "sleep"],
    ])
    runner = jobs.Runner(tmp_path)
    yield runner
    runner.shutdown()


def test_a_job_keeps_its_output_and_reports_failure(runner):
    job = runner.start("hello", {"uid": "7"})
    assert _wait(job) == "failed"
    snapshot = job.snapshot()
    assert snapshot["label"] == "Saying hello to #7"
    # What it works on, so the page can tell which application is busy.
    assert snapshot["uid"] == "7" and snapshot["url"] == ""
    assert sorted(snapshot["lines"]) == ["one", "two"] and snapshot["next"] == 2
    assert job.snapshot(since=2)["lines"] == []


def test_one_job_at_a_time_and_it_can_be_stopped(runner):
    job = runner.start("sleep", {})
    with pytest.raises(SetupError, match="Still busy"):
        runner.start("hello", {})
    runner.cancel()
    assert _wait(job) == "cancelled"
    assert _wait(runner.start("hello", {"uid": "1"})) == "failed"


def test_the_command_is_shown_as_the_docs_write_it(monkeypatch):
    monkeypatch.setattr(jobs, "PYTHON_SHOWN", jobs.python_shown("posix"))
    assert jobs.shown("search_practice", {}) == "./.venv/bin/python -m jobradar.search.main --dry-run"
    assert jobs.shown("draft", {"url": "https://jobs.example.ch/1?a=b&c=d", "full": True}) == (
        "./.venv/bin/python -m jobradar.apply --url 'https://jobs.example.ch/1?a=b&c=d' --full"
    )
    assert jobs.shown("submitted", {"uid": "7"}) == "./.venv/bin/python -m jobradar.apply --mark-submitted 7"


def test_windows_is_shown_its_own_venv_path_unquoted(monkeypatch):
    # Unquoted: PowerShell won't run a quoted path without `&` in front.
    monkeypatch.setattr(jobs, "PYTHON_SHOWN", jobs.python_shown("nt"))
    assert jobs.shown("search_practice", {}) == r".\.venv\Scripts\python -m jobradar.search.main --dry-run"
    assert jobs.python_shown("posix") == "./.venv/bin/python"

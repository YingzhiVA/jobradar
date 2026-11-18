"""Run a jobradar command for the browser and keep what it prints.

Every action the page offers is one of the commands in ``ACTIONS``, started
with the interpreter this server runs under. Nothing the browser sends reaches
a shell: an action name picks the argument list, and the only free values are
an application id (digits), a month and a posting URL, each checked.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .setup import SetupError

_UID = re.compile(r"^\d{1,6}$")
_URL = re.compile(r"^https?://[^\s]+$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_LOG_PREFIX = re.compile(r"^(\d\d:\d\d:\d\d )?(INFO|WARNING|ERROR|DEBUG) [\w.]+: ")

APPLY = "jobradar.apply"
SEARCH = "jobradar.search.main"


def _uid(params: dict) -> str:
    uid = str(params.get("uid", ""))
    if not _UID.match(uid):
        raise SetupError("That is not an application number.")
    return uid


def _url(params: dict) -> str:
    url = str(params.get("url", "")).strip()
    if not _URL.match(url) or len(url) > 2000:
        raise SetupError("Paste the full web address of the posting, starting with https://")
    return url


def _month(params: dict) -> str:
    month = str(params.get("month", ""))
    if not _MONTH.match(month):
        raise SetupError("Give the month as YYYY-MM.")
    return month


@dataclass(frozen=True)
class Action:
    label: str  # what the page shows while it runs; {uid} and {url} are filled in
    module: str
    args: object  # callable(params) -> list[str]
    background: bool = True  # False: finishes in a moment, the request waits for it


ACTIONS: dict[str, Action] = {
    "search_practice": Action("Practice run on sample postings", SEARCH, lambda p: ["--dry-run"]),
    "search": Action("Searching for new postings", SEARCH, lambda p: []),
    "draft": Action(
        "Drafting an application", APPLY,
        lambda p: ["--url", _url(p)] + (["--full"] if p.get("full") else []),
    ),
    "upgrade": Action("Re-drafting #{uid} with a cover letter", APPLY, lambda p: ["--upgrade", _uid(p)]),
    "prep": Action("Preparing the interview sheet for #{uid}", APPLY, lambda p: ["--prep", _uid(p)]),
    "pdf": Action("Making the PDFs for #{uid}", APPLY, lambda p: ["--pdf", _uid(p)]),
    "lint": Action("Checking #{uid} before sending", APPLY, lambda p: ["--lint", _uid(p)]),
    "submitted": Action("", APPLY, lambda p: ["--mark-submitted", _uid(p)], background=False),
    "rejected": Action("", APPLY, lambda p: ["--mark-rejected", _uid(p)], background=False),
    "withdrawn": Action("", APPLY, lambda p: ["--mark-withdrawn", _uid(p)], background=False),
    "offer": Action("", APPLY, lambda p: ["--mark-offer", _uid(p)], background=False),
    "rav": Action("", APPLY, lambda p: ["--rav", _month(p)], background=False),
    "rav_filed": Action("", APPLY, lambda p: ["--rav-filed", _month(p)], background=False),
}


def clean(line: str) -> str:
    """A log line without its "INFO jobradar.module:" prefix."""
    return _LOG_PREFIX.sub("", line.rstrip())


def _command(action: Action, params: dict) -> list[str]:
    return [sys.executable, "-m", action.module, *action.args(params)]


# How the docs write the interpreter. The page shows each command it runs in
# this form, so that what is learnt there can be typed in a terminal. The docs
# say `uv run python`, the same on every system; without uv (installed with
# pip, see the README) it is the venv's own python. A venv on Windows keeps it
# under Scripts\ rather than bin/; the leading .\ makes the same text run in
# PowerShell and in cmd.
def python_shown(os_name: str = os.name, uv: bool | None = None) -> str:
    if uv is None:
        uv = shutil.which("uv") is not None
    if uv:
        return "uv run python"
    return r".\.venv\Scripts\python" if os_name == "nt" else "./.venv/bin/python"


PYTHON_SHOWN = python_shown()


def shown(name: str, params: dict) -> str:
    """The command an action runs, as one would type it."""
    action = ACTIONS[name]
    # The interpreter stays outside shlex: it would quote the Windows form for
    # its backslashes, and PowerShell won't run a quoted path without `&`.
    return f"{PYTHON_SHOWN} {shlex.join(['-m', action.module, *action.args(params)])}"


def _env() -> dict:
    # Unbuffered, so lines arrive as they are printed and not when the command
    # ends; UTF-8, because Windows would otherwise encode output in a code page
    # the spinner and the umlauts don't fit in.
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


def run_quick(root: Path, name: str, params: dict) -> dict:
    """Run a command that finishes in a moment and return what it said."""
    action = ACTIONS[name]
    done = subprocess.run(
        _command(action, params), cwd=root, env=_env(), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    lines = [clean(l) for l in (done.stdout + done.stderr).splitlines() if l.strip()]
    return {"ok": done.returncode == 0, "output": "\n".join(lines), "command": shown(name, params)}


@dataclass
class Job:
    id: int
    action: str
    label: str
    process: subprocess.Popen
    command: str = ""
    # The application or posting it works on, so the page can tell which
    # one is busy: an id, an address, or both empty.
    uid: str = ""
    url: str = ""
    started: float = field(default_factory=time.time)
    ended: float | None = None
    lines: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def state(self) -> str:
        code = self.process.poll()
        if code is None or self.ended is None:
            return "running"
        if self.cancelled:
            return "cancelled"
        return "done" if code == 0 else "failed"

    def snapshot(self, since: int = 0) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "label": self.label,
            "command": self.command,
            "uid": self.uid,
            "url": self.url,
            "state": self.state,
            "seconds": int((self.ended or time.time()) - self.started),
            "lines": self.lines[since:],
            "next": len(self.lines),
        }


class Runner:
    """One command at a time: two runs would both write the seen store and
    the tracker, and each is paid for."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._count = 0

    @property
    def job(self) -> Job | None:
        return self._job

    def start(self, name: str, params: dict) -> Job:
        action = ACTIONS[name]
        command = _command(action, params)  # validates before anything starts
        with self._lock:
            if self._job is not None and self._job.state == "running":
                raise SetupError(f"Still busy: {self._job.label}. Wait for it to finish.")
            process = subprocess.Popen(
                command, cwd=self.root, env=_env(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            self._count += 1
            label = action.label.format(uid=params.get("uid", ""), url=params.get("url", ""))
            job = Job(
                self._count, name, label, process, command=shown(name, params),
                uid=str(params.get("uid") or ""), url=str(params.get("url") or "").strip(),
            )
            self._job = job
        threading.Thread(target=self._read, args=(job,), daemon=True).start()
        return job

    @staticmethod
    def _read(job: Job) -> None:
        assert job.process.stdout is not None
        for line in job.process.stdout:
            if line.strip():
                job.lines.append(clean(line))
        job.process.wait()
        job.ended = time.time()

    def cancel(self) -> None:
        job = self._job
        if job is not None and job.state == "running":
            job.cancelled = True
            job.process.terminate()

    def shutdown(self) -> None:
        self.cancel()

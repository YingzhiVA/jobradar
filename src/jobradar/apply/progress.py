"""Say what the pipeline is doing while it does it.

A single application costs one page fetch and three to five model calls, and
the writing calls are the slow ones: a full CV plus a cover letter is ~12k
output tokens, and a German posting adds a translation call on top. Nothing
in that reaches the terminal until it is over, so the run used to print the
SDK's "HTTP Request: POST /v1/messages 200 OK" and then sit silent for
minutes - indistinguishable from a hang, and the natural response to a
suspected hang is Ctrl-C, which throws away the work already paid for.

Two things fix that, and this module is the first: every slow step announces
itself before it starts and reports how long it took when it ends, and while
it runs a ticker proves the process is still alive. The second is streaming
(see tailor.py): the model's output arrives token by token, so the ticker can
show the answer actually being written rather than just a clock.

Two renderers, picked by whether stderr is a terminal:

- Interactive: one repainted line carrying a spinner, the step, the elapsed
  time and how much text has streamed back. It is transient - when the step
  ends the line is replaced by a permanent one-line summary, so the scrollback
  reads as a clean timeline with no spinner debris.
- Piped or redirected (CI logs, `tee`, a cron mail): no repainting, since
  carriage returns in a log file are noise. The step start and end are logged,
  and a heartbeat line is logged every JOBRADAR_PROGRESS_INTERVAL seconds
  while it runs, so a stuck run is visible in the log too.

Set JOBRADAR_PROGRESS=0 to switch both off and keep only the plain log lines.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

# How often the piped-output renderer says "still running". Long enough not to
# spam a CI log, short enough that a watched run never looks dead for long.
HEARTBEAT_SECONDS = float(os.environ.get("JOBRADAR_PROGRESS_INTERVAL", "20"))
_REPAINT_SECONDS = 0.12  # spinner frame rate on a terminal

_SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_ASCII = "|/-\\"


def _spinner_frames() -> str:
    """Braille dots where the terminal can encode them, ASCII everywhere else
    (a progress indicator that raises UnicodeEncodeError is worse than none)."""
    encoding = getattr(sys.stderr, "encoding", None) or ""
    try:
        _SPINNER_UNICODE.encode(encoding)
    except (LookupError, UnicodeError):
        return _SPINNER_ASCII
    return _SPINNER_UNICODE


def enabled() -> bool:
    return os.environ.get("JOBRADAR_PROGRESS", "1") not in ("0", "false", "no")


def _is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):  # closed or replaced stream
        return False


def fmt_duration(seconds: float) -> str:
    """Compact and unpadded: 4s, 48s, 2m12s, 1h03m."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_count(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


class Stage:
    """One slow step, and the counters a renderer reads off it.

    `advance` is what a streaming model call feeds its text deltas into; it is
    called from the SDK's response loop, so it stays a bounded increment under
    a lock rather than doing any drawing of its own.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._started = time.monotonic()
        self._chars = 0
        self._lock = threading.Lock()

    def advance(self, text: str) -> None:
        with self._lock:
            self._chars += len(text)

    @property
    def chars(self) -> int:
        with self._lock:
            return self._chars

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def summary(self) -> str:
        """The trailing "(48s, 11.9k chars)" both renderers append."""
        parts = [fmt_duration(self.elapsed)]
        chars = self.chars
        if chars:
            parts.append(f"{fmt_count(chars)} chars")
        return ", ".join(parts)


# --- the transient terminal line -------------------------------------------
#
# Module state rather than per-Stage, because the log handler filter installed
# by install() has to be able to wipe whatever line is currently painted
# without knowing which stage painted it.

_line_lock = threading.RLock()
_line_painted = False


def _write(text: str) -> None:
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except (OSError, ValueError):  # stderr closed mid-run; progress is optional
        pass


def _fit(head: str, label: str, tail: str, width: int) -> str:
    """Assemble the live line, shortening the label rather than the counters.

    The counters are the whole point of the line - they are what distinguishes
    "working" from "wedged" - so a narrow terminal loses the middle of the
    step name instead. If even that will not fit, the line is cut flat: a
    clipped line still beats a wrapped one, which scrolls and defeats the
    in-place repaint.
    """
    room = width - len(head) - len(tail)
    if room >= len(label):
        return head + label + tail
    if room >= 12:  # enough of the label left to still identify the step
        keep = room - 3
        return head + label[: keep - keep // 3] + "..." + label[-(keep // 3) :] + tail
    return (head + label + tail)[:width]


def _paint(head: str, label: str, tail: str) -> None:
    global _line_painted
    width = max(0, shutil.get_terminal_size((80, 24)).columns - 1)
    with _line_lock:
        _write("\r\x1b[2K" + _fit(head, label, tail, width))
        _line_painted = True


def clear_line() -> None:
    """Wipe the transient line, if one is up. Safe to call at any time."""
    global _line_painted
    with _line_lock:
        if _line_painted:
            _write("\r\x1b[2K")
            _line_painted = False


def _clearing_emit(handler: logging.Handler):
    """Wrap a handler's emit so the record lands on a clean line.

    The wipe and the write have to be one atomic step, which is why this
    wraps emit rather than filtering. A logging Filter runs just *before*
    emit and outside the handler's lock, so the ticker could repaint in the
    gap between the wipe and the write - and its next repaint begins by
    erasing the line, taking the log record with it. A log line that a
    spinner ate is a worse bug than the silence this module exists to fix.
    """
    original = handler.emit

    def emit(record: logging.LogRecord) -> None:
        with _line_lock:  # reentrant: paint/clear may be called underneath
            clear_line()
            original(record)

    emit._jobradar_wrapped = original  # type: ignore[attr-defined]
    return emit


def install() -> None:
    """Teach the root logger's handlers to step around the live line.

    Idempotent, and a no-op when there is no live line to step around (output
    is not a terminal, or progress is switched off).
    """
    if not (enabled() and _is_tty()):
        return
    for handler in logging.getLogger().handlers:
        if not hasattr(handler.emit, "_jobradar_wrapped"):
            handler.emit = _clearing_emit(handler)  # type: ignore[method-assign]


class _Ticker(threading.Thread):
    """Keeps the stage visible while it runs, in whichever way fits stderr."""

    def __init__(self, stage: Stage, *, live: bool) -> None:
        super().__init__(daemon=True)
        self._stage = stage
        self._live = live
        self._done = threading.Event()

    def run(self) -> None:
        if self._live:
            frames = _spinner_frames()
            tick = 0
            while not self._done.wait(_REPAINT_SECONDS):
                _paint(
                    f"{frames[tick % len(frames)]} ",
                    self._stage.label,
                    f"  {self._stage.summary()}",
                )
                tick += 1
        else:
            while not self._done.wait(HEARTBEAT_SECONDS):
                logger.info(
                    "still running: %s (%s)", self._stage.label, self._stage.summary()
                )

    def stop(self) -> None:
        self._done.set()
        self.join(timeout=1.0)


@contextmanager
def step(label: str) -> Iterator[Stage]:
    """Run a slow step under a progress indicator.

    Yields the Stage: pass its `advance` to a streaming model call and the
    indicator counts the response as it arrives.
    """
    stage = Stage(label)
    live = enabled() and _is_tty()
    # On a terminal the spinner is the start announcement, so logging one too
    # would just be a line the spinner immediately covers. Everywhere else -
    # and whenever progress is switched off - it has to be logged, or the
    # start of a slow step is again invisible.
    if not live:
        logger.info("starting: %s", label)
    ticker = _Ticker(stage, live=live) if enabled() else None
    if ticker is not None:
        ticker.start()
    try:
        yield stage
    except BaseException:
        if ticker is not None:
            ticker.stop()
        clear_line()
        # Counters included: how far a step got before it died is the first
        # thing worth knowing about it.
        logger.info("failed: %s (after %s)", label, stage.summary())
        raise
    else:
        if ticker is not None:
            ticker.stop()
        clear_line()
        # The permanent line, replacing the transient one: "starting" and
        # "finished" rather than a bare label at both ends, so a glance at the
        # log tells you which steps are done and which one you are waiting on.
        logger.info("finished: %s (%s)", label, stage.summary())

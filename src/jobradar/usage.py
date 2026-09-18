"""Aggregate and log prompt-cache usage across a run's LLM calls.

Lets a stage confirm the cached profile block is actually being reused
(cache_read > 0 after the first call) rather than silently re-sent at full price
every call — e.g. if the profile ever drops below a model's cache minimum, the
log shows cache_read stuck at 0.
"""

from __future__ import annotations

import contextlib
import logging
import threading


def accumulate(acc: dict, response, lock: threading.Lock | None = None) -> None:
    """Add one response's usage to the running accumulator (mutates acc).

    Pass `lock` when several threads share one accumulator (see
    matching.score_postings): each line below is a read-modify-write, so
    without it concurrent callers interleave and silently lose counts —
    which would make the cache summary under-report rather than fail loudly.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    with lock or contextlib.nullcontext():
        acc["calls"] = acc.get("calls", 0) + 1
        acc["cache_creation"] = acc.get("cache_creation", 0) + (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        acc["cache_read"] = acc.get("cache_read", 0) + (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        acc["input"] = acc.get("input", 0) + (getattr(usage, "input_tokens", 0) or 0)


def log_summary(logger: logging.Logger, stage: str, acc: dict) -> None:
    """Log a one-line cache summary for a stage (no-op if no calls were made)."""
    if not acc.get("calls"):
        return
    logger.info(
        "%s cache: %d calls | %d tokens cache-created, %d cache-read, %d uncached input",
        stage,
        acc["calls"],
        acc.get("cache_creation", 0),
        acc.get("cache_read", 0),
        acc.get("input", 0),
    )

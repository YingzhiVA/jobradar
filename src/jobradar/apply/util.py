"""Small helpers shared by the apply pipeline."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable


def drain_stream(stream: Any, on_delta: Callable[[str], None] | None = None) -> None:
    """Consume a `client.messages.stream(...)` response to completion, handing
    each text delta to `on_delta`.

    Every model call in this pipeline wants the whole answer before it can do
    anything with it, so the deltas are only ever a progress signal (see
    apply/progress.py). Draining has to happen regardless: `get_final_message`
    blocks until the stream is read, and an un-consumed stream never yields
    the parsed output at all.
    """
    for chunk in stream.text_stream:
        if on_delta is not None:
            on_delta(chunk)


def normalize_url(url: str) -> str:
    """Canonical form used as the dedup/tracker key. Deliberately light:
    only whitespace, the fragment, and a trailing slash are stripped, because
    ATSs encode the real job id in the path or query and stripping more could
    merge two different postings (same reasoning as models.posting_id).
    """
    url = (url or "").strip()
    url = url.split("#", 1)[0]
    return url.rstrip("/")


def slugify(text: str, max_len: int = 40) -> str:
    """Filesystem-safe lowercase slug for folder names."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "unknown"

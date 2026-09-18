"""Fetch a job posting URL and reduce it to visible text.

Reuses liveness.py's SSRF guard (queue URLs are user-supplied, but they get
pasted from all over the web, so the same hardening is cheap insurance) and
its manual redirect-following so every hop is re-checked. Unlike liveness,
this module actually reads the body - bounded, so a hostile or huge page
can't be pulled fully into memory.

HTML-to-text is deliberately dumb (stdlib HTMLParser, drop script/style,
newline on block tags): the Haiku extraction pass downstream is what turns
the noisy text into a clean job description, so this layer only needs to
not lose the content. JS-rendered ATSs (e.g. Workday) ship almost no
visible text in the raw HTML - the pipeline detects that by length and
falls back to asking the user to paste the JD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from ..search.liveness import _HEADERS, _MAX_REDIRECTS, _is_public_http_url

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_MAX_BYTES = 2_000_000  # bound how much of a page we will ever read
_DEAD_STATUSES = {404, 410}

# Below this many characters of visible text the page is almost certainly a
# JS-rendered shell (or an error page), not a job description.
MIN_USABLE_CHARS = 500

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1] != "":
                out.append("")  # collapse runs of blank lines to one
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML shouldn't kill the run
        pass
    return parser.text()


@dataclass
class FetchOutcome:
    url: str  # the URL that was requested
    final_url: str | None = None  # after redirects, when the fetch succeeded
    text: str | None = None  # visible page text
    # Raw markup, kept alongside the flattened text for callers that need the
    # structure (evidence.py converts hand-written HTML to Markdown, where the
    # headings, tables and code blocks html_to_text throws away are the point).
    html: str | None = None
    # True when the body hit _MAX_BYTES and was cut off mid-stream. The JD
    # extraction pass downstream tolerates a clipped posting; evidence.py does
    # not, since a truncated article would be written out as a whole one.
    truncated: bool = False
    error: str | None = None  # human-readable reason when text is None

    @property
    def usable(self) -> bool:
        return self.text is not None and len(self.text) >= MIN_USABLE_CHARS


def fetch_page(url: str, client: httpx.Client | None = None) -> FetchOutcome:
    own_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers=_HEADERS)
    try:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not _is_public_http_url(current):
                return FetchOutcome(url, error=f"unsafe or unresolvable URL: {current}")
            try:
                with client.stream("GET", current, follow_redirects=False) as resp:
                    nxt = resp.next_request
                    if nxt is not None:
                        current = str(nxt.url)
                        continue
                    if resp.status_code in _DEAD_STATUSES:
                        return FetchOutcome(url, error=f"posting gone (HTTP {resp.status_code})")
                    if resp.status_code >= 400:
                        return FetchOutcome(url, error=f"fetch failed (HTTP {resp.status_code})")
                    chunks: list[bytes] = []
                    read = 0
                    truncated = False
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        read += len(chunk)
                        if read >= _MAX_BYTES:
                            truncated = True
                            break
                    encoding = resp.charset_encoding or "utf-8"
                    html = b"".join(chunks).decode(encoding, errors="replace")
                    return FetchOutcome(
                        url, final_url=current, text=html_to_text(html), html=html,
                        truncated=truncated,
                    )
            except httpx.HTTPError as exc:
                return FetchOutcome(url, error=f"network error: {exc}")
        return FetchOutcome(url, error="too many redirects")
    finally:
        if own_client:
            client.close()

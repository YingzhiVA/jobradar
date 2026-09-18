"""Ingest the candidate's published write-ups (field notes, project posts) from
their portfolio site into profile/evidence/ as Markdown.

Run on demand, whenever the site gains a note:

    python -m jobradar.evidence --site https://you.github.io/notes/

Why a crawler rather than a copy of the source repo: the published page is the
canonical version, it is what a recruiter following the link actually reads,
and depending on it keeps jobradar independent of how that site is built.

Why a structure-aware converter rather than apply/fetch.py's html_to_text:
these pages are hand-written prose where the headings, tables, code blocks and
figure descriptions carry the evidence. Flattening them to visible text throws
away exactly the parts that make a note read as demonstrated depth rather than
as a wall of words. The conversion is deliberately narrow - it handles the
semantic subset a hand-written article uses and ignores the rest.

Only pages tagged `<meta property="og:type" content="article">` are ingested;
collection and section indexes are crawled for links but never written, since
they are navigation rather than evidence.

The result feeds the apply pipeline only (see matching.load_evidence) - not
daily scoring, where a few thousand words of technical detail would be paid
for on every posting for a signal a short CV line already carries.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import ParseResult, urldefrag, urljoin, urlparse

import httpx
from dotenv import load_dotenv

from .apply.fetch import _TIMEOUT, fetch_page
from .search.liveness import _HEADERS

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]  # project root, two levels above src/jobradar/
DEFAULT_OUT_DIR = ROOT / "profile" / "evidence"

# Generous for a personal site's writing section, tight enough that pointing
# this at the wrong URL fails fast instead of crawling a whole domain.
MAX_PAGES = 60
MAX_DEPTH = 3

# Presentation-only markup. `canvas` and `svg` matter here: several notes draw
# their figures in them, and the accessible description on the enclosing
# <figure> is the part worth keeping - see _NoteParser.handle_starttag.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe", "form"}

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key == name:
            return value or ""
    return ""


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    return set(_attr(attrs, "class").split())


def _normalize_url(url: str) -> str:
    """Drops the fragment and query, and collapses `.../index.html` to `.../`,
    so the same page reached two ways is crawled once."""
    clean, _ = urldefrag(url)
    clean = clean.split("?", 1)[0]
    if clean.endswith("/index.html"):
        clean = clean[: -len("index.html")]
    return clean


class _NoteParser(HTMLParser):
    """Converts one hand-written article page to Markdown.

    Two things make this tractable where general HTML-to-Markdown is not.
    First, the site chrome is skipped structurally: everything before the
    article's <header> is navigation, so nothing is emitted until one is seen.
    Second, the standfirst fields (eyebrow, title, dek, meta) are routed to
    named slots instead of the body, so the writer can build a clean header
    block rather than re-deriving it from the first few paragraphs.
    """

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.og_type = ""
        self.page_title = ""
        self.description = ""
        self.title = ""  # the article's own <h1>
        self.eyebrow = ""
        self.dek = ""
        self.meta = ""
        self.links: list[str] = []

        self._blocks: list[str] = []
        self._items: set[int] = set()  # indexes of self._blocks that are list items
        self._buf: list[str] = []
        self._slot = ""  # when set, the buffer lands in a named field, not the body
        self._skip = 0
        # Tags of open elements whose content is discarded entirely (per-note
        # byline boilerplate, next/previous navigation cards).
        self._drop: list[str] = []
        self._in_head = False
        self._in_title = False
        self._started = False
        self._heading = 0
        self._quote_depth = 0
        self._pre: list[str] | None = None
        self._lang = ""
        self._lists: list[list] = []  # [tag, counter] per open list
        self._li = 0
        self._li_bold = False
        self._href = ""
        self._href_mark = 0  # buffer position of the open "["
        self._tag_span = False
        self._div_depth = 0
        # Div depth of an open "fold" - a container whose whole content is one
        # logical line (a .callout aside, a .readout label/value pair). -1 when
        # none is open. _fold_quote marks the ones that read as an aside.
        self._fold_div = -1
        self._fold_quote = False
        self._in_figure = 0
        self._fig_label = ""
        self._fig_caption = ""
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._head_rows = 0
        self._in_thead = False
        self._caption = ""

    # -- buffer plumbing -------------------------------------------------

    def _take(self) -> str:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        return text

    def _emit(self) -> None:
        """Finalizes the inline buffer into a body block, or into the named
        slot when one is open."""
        text = self._take()
        slot, self._slot = self._slot, ""
        heading, self._heading = self._heading, 0
        # _quote is a depth, not a one-shot flag: a <blockquote> holding two
        # <p>s used to lose its marker on everything after the first flush.
        quote = self._quote_depth > 0
        if not text:
            return
        if slot:
            setattr(self, slot, text)
        elif heading:
            self._blocks.append(f"{'#' * heading} {text}")
        elif quote:
            self._blocks.append("\n".join(f"> {line}" for line in text.split("\n")))
        else:
            self._blocks.append(text)

    def _flush_item(self) -> None:
        """Finalizes the buffer as a bullet of the innermost open list."""
        text = self._take()
        if not text or not self._lists:
            return
        self._lists[-1][1] += 1
        tag_name, count = self._lists[-1]
        marker = f"{count}. " if tag_name == "ol" else "- "
        indent = "  " * (len(self._lists) - 1)
        self._items.add(len(self._blocks))
        self._blocks.append(f"{indent}{marker}{text}")

    def _space(self) -> None:
        if self._buf and not "".join(self._buf[-1:]).endswith(" "):
            self._buf.append(" ")

    # -- tags ------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - stdlib signature
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return

        if tag == "head":
            self._in_head = True
        if self._in_head:
            if tag == "title":
                self._in_title = True
            elif tag == "meta":
                key = _attr(attrs, "property") or _attr(attrs, "name")
                if key == "og:type":
                    self.og_type = _attr(attrs, "content")
                elif key in ("description", "og:description") and not self.description:
                    self.description = _attr(attrs, "content")
            return

        href = _attr(attrs, "href")
        if tag == "a" and href:
            self.links.append(urljoin(self.base_url, href))

        if not self._started:
            # Site chrome precedes the article; the hero header opens it.
            if tag in ("header", "main", "article"):
                self._started = True
            return

        cls = _classes(attrs)

        if (tag == "p" and "sig" in cls) or (tag == "a" and "nextup" in cls):
            self._drop.append(tag)
            return
        if self._drop:
            return

        if tag in _HEADINGS:
            if self._li:
                self._buf.append("**")  # a heading inside a list item is its lead-in
                self._li_bold = True
            elif tag == "h1" and not self.title:
                # The article's own title, not a body heading: the writer puts
                # it back at the top with the source URL beside it.
                self._emit()
                self._slot = "title"
            else:
                self._emit()
                self._heading = _HEADINGS[tag]
            return
        if tag == "p":
            # A paragraph inside a list item, a callout or a blockquote is
            # part of that one block, not a new one, so it folds into the
            # buffer instead.
            if self._li or self._fold_div >= 0 or self._quote_depth:
                self._space()
            else:
                self._emit()
                self._slot = "dek" if "dek" in cls else ""
            return
        if tag == "blockquote":
            self._emit()
            self._quote_depth += 1
            return
        if tag in ("ul", "ol"):
            # A list opening inside an open <li> is a sub-list: the parent's own
            # text has to become its bullet FIRST, or it flushes as a stray
            # paragraph and loses its marker entirely.
            if self._li:
                self._flush_item()
            else:
                self._emit()
            self._lists.append([tag, 0])
            return
        if tag == "li":
            self._emit()
            self._li += 1
            return
        if tag == "pre":
            self._emit()
            self._pre = []
            return
        if tag == "code" and self._pre is None:
            self._buf.append("`")
            return
        if tag in ("strong", "b"):
            self._buf.append("**")
            return
        if tag in ("em", "i"):
            self._buf.append("*")
            return
        if tag == "a":
            # Only a real link opens a Markdown label; a bare in-page anchor
            # (<a id="fig-1"></a>) would otherwise inject a literal "[]".
            if href:
                self._href = urljoin(self.base_url, href)
                self._href_mark = len(self._buf)
                self._buf.append("[")
            return
        if tag == "img":
            alt = _attr(attrs, "alt") or "figure"
            src = urljoin(self.base_url, _attr(attrs, "src"))
            self._emit()
            self._blocks.append(f"![{alt}]({src})")
            return
        if tag == "br":
            self._space()
            return
        if tag == "figure":
            self._emit()
            # The accessible description is the only prose form of a figure the
            # site draws in <svg>/<canvas>, so it is the figure's real content.
            self._in_figure += 1
            self._fig_label = " ".join(_attr(attrs, "aria-label").split())
            self._fig_caption = ""
            return
        if tag == "figcaption":
            self._emit()
            self._slot = "_fig_caption"
            return
        if tag == "table":
            self._emit()
            self._table = []
            self._head_rows = 0
            self._caption = ""
            return
        if tag == "caption" and self._table is not None:
            self._emit()
            self._slot = "_caption"
            return
        if tag == "thead":
            self._in_thead = True
            return
        if tag == "tr" and self._table is not None:
            self._row = []
            return
        if tag in ("th", "td") and self._row is not None:
            self._buf = []
            return
        if tag == "span":
            if "eyebrow" in cls:
                self._emit()
                self._slot = "eyebrow"
            elif "snum" in cls:
                self._emit()
                self._buf.append("**")
                self._li_bold = True
            elif "lang" in cls:
                self._emit()
                self._slot = "_lang"
            elif ("tag" in cls and self._fold_div >= 0) or "k" in cls:
                # A label: the callout's tag ("Scope"), or the term half of a
                # definition list. Both read as a bold lead-in.
                self._buf.append("**")
                self._tag_span = True
            return
        if tag == "div":
            self._div_depth += 1
            # A plain <div> ends the preceding block. Without this, sibling divs
            # (the .eq-row equation rows) run together into one line. List items
            # and callouts wrap their content in divs, so they are exempt.
            if not self._li and self._fold_div < 0 and self._table is None:
                self._emit()
            if "meta" in cls and not self.meta:
                self._emit()
                self._slot = "meta"
            elif "lbl" in cls and self._fold_div >= 0:
                self._buf.append("**")  # the label half of a readout
                self._tag_span = True
            elif cls & {"callout", "readout"} and self._fold_div < 0:
                self._emit()
                self._fold_div = self._div_depth
                self._fold_quote = "callout" in cls
            return

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "head":
            self._in_head = False
            return
        if self._in_head:
            if tag == "title":
                self._in_title = False
            return
        if not self._started:
            return
        if self._drop:
            if self._drop[-1] == tag:
                self._drop.pop()
            return

        if tag in _HEADINGS:
            if self._li_bold:
                self._buf.append("**")
                self._li_bold = False
                if self._li:
                    self._buf.append(" - ")
            else:
                self._emit()
            return
        if tag == "span" and self._li_bold and not self._li:
            # A section kicker (span.snum) standing on its own above the heading.
            self._buf.append("**")
            self._li_bold = False
            self._emit()
            return
        if tag == "p":
            if not self._li and self._fold_div < 0 and not self._quote_depth:
                self._emit()
            return
        if tag == "blockquote":
            self._emit()
            self._quote_depth = max(0, self._quote_depth - 1)
            return
        if tag in ("ul", "ol"):
            self._emit()
            if self._lists:
                self._lists.pop()
            return
        if tag == "li":
            self._li = max(0, self._li - 1)
            self._flush_item()
            return
        if tag == "pre":
            code = "".join(self._pre or []).strip("\n")
            self._pre = None
            lang, self._lang = self._lang, ""
            if code:
                self._blocks.append(f"```{lang}\n{code}\n```")
            return
        if tag == "code" and self._pre is None:
            self._buf.append("`")
            return
        if tag in ("strong", "b"):
            self._buf.append("**")
            return
        if tag in ("em", "i"):
            self._buf.append("*")
            return
        if tag == "a":
            if self._href:
                # An anchor with no visible text is a target, not a link:
                # emitting it would leave a bare "[](url)" in the prose.
                if "".join(self._buf[self._href_mark + 1:]).strip():
                    self._buf.append(f"]({self._href})")
                else:
                    del self._buf[self._href_mark:]
                self._href = ""
            return
        if tag == "figure":
            self._in_figure = max(0, self._in_figure - 1)
            self._emit()
            parts = [p for p in (self._fig_caption, self._fig_label) if p]
            # Caption and description often say the same thing twice; keep the
            # longer one when one contains the other.
            if len(parts) == 2 and parts[0].lower() in parts[1].lower():
                parts = [parts[1]]
            if parts:
                self._blocks.append("\n".join(f"> Figure: {p}" for p in parts))
            self._fig_label = self._fig_caption = ""
            return
        if tag == "figcaption":
            self._emit()
            return
        if tag == "caption" and self._table is not None:
            self._emit()
            return
        if tag == "thead":
            self._in_thead = False
            return
        if tag in ("th", "td") and self._row is not None:
            self._row.append(self._take().replace("|", "\\|"))
            return
        if tag == "tr" and self._table is not None:
            if self._row:
                self._table.append(self._row)
                if self._in_thead:
                    self._head_rows += 1
            self._row = None
            return
        if tag == "table":
            self._render_table()
            return
        if tag == "span" and self._tag_span:
            self._buf.append("** - ")
            self._tag_span = False
            return
        # Only slots a <span> itself opened close here. The meta slot is opened
        # by its wrapping <div>, and its inner spans are the parts of one line.
        if tag == "span" and self._slot in ("eyebrow", "_lang"):
            self._emit()
            return
        if tag == "div":
            if self._tag_span:
                self._buf.append("** - ")
                self._tag_span = False
            elif self._slot == "meta":
                self._emit()
            elif self._fold_div == self._div_depth:
                self._fold_div = -1
                # A callout reads as an aside; a readout is just a line.
                self._quote_depth += self._fold_quote
                self._emit()
                self._quote_depth -= self._fold_quote
                self._fold_quote = False
            self._div_depth = max(0, self._div_depth - 1)
            return

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        # Both halves: a self-closed stateful tag (<a/>, <li/>, <div/>) would
        # otherwise push state nothing ever pops, corrupting the rest of the
        # document. Void elements (img, br, hr) match no end-tag branch, so
        # running the close half costs them nothing.
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip or self._drop:
            return
        if self._in_title:
            self.page_title += data
            return
        if self._in_head or not self._started:
            return
        if self._pre is not None:
            self._pre.append(data)
            return
        if self._in_figure and self._slot != "_fig_caption":
            # Loose text inside a <figure> is the chart's own furniture - legend
            # chips, axis labels - drawn beside the canvas. The caption and the
            # aria-label are the figure's prose; this is not.
            return
        if not data.strip():
            self._space()
            return
        if self._slot == "meta":
            # The meta line's spans butt up against each other in the source
            # ("Field note" "·" "~6 min read"); keep them apart.
            self._space()
        self._buf.append(data)

    # -- output ----------------------------------------------------------

    def _render_table(self) -> None:
        rows, self._table = self._table or [], None
        caption, self._caption = self._caption, ""
        if not rows:
            return
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        head_rows = self._head_rows or 1
        self._head_rows = 0
        header, body = rows[:head_rows], rows[head_rows:]
        lines = ["| " + " | ".join(header[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines += ["| " + " | ".join(row) + " |" for row in header[1:] + body]
        if caption:
            lines.insert(0, f"*{caption}*\n")
        self._blocks.append("\n".join(lines))

    def markdown(self) -> str:
        self._emit()
        parts: list[str] = []
        prev_item = False
        for index, block in enumerate(self._blocks):
            if not block.strip():
                continue
            is_item = index in self._items
            if parts:
                # Consecutive list items stay tight; everything else gets the
                # blank line that separates Markdown blocks.
                parts.append("\n" if is_item and prev_item else "\n\n")
            parts.append(block)
            prev_item = is_item
        return "".join(parts)


@dataclass
class Note:
    """One ingested write-up."""

    url: str
    slug: str
    title: str
    eyebrow: str = ""
    dek: str = ""
    meta: str = ""
    description: str = ""
    body: str = ""
    links: list[str] = field(default_factory=list)
    is_article: bool = False

    def render(self) -> str:
        """The Markdown written to profile/evidence/<slug>.md.

        The header block is what the apply prompts key on: the source URL makes
        the note linkable in a cover letter, and the meta line carries the
        publication caveats (notably "details generalized") that the generated
        documents must not undo.
        """
        lines = [f"# {self.title}", ""]
        lines.append(f"- Source: {self.url}")
        if self.eyebrow:
            lines.append(f"- Series: {self.eyebrow}")
        if self.meta:
            lines.append(f"- Published as: {self.meta}")
        lines.append("")
        summary = self.dek or self.description
        if summary:
            lines += [f"> {summary}", ""]
        lines.append(self.body)
        return "\n".join(lines).rstrip() + "\n"


def _slug_for(url: str, root_path: str) -> str:
    path = urlparse(url).path
    if path.startswith(root_path):
        path = path[len(root_path):]
    parts = [re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in path.split("/")]
    slug = "-".join(p for p in parts if p and p != "index-html")
    return slug or "index"


def parse_page(html: str, url: str, root_path: str = "/") -> Note:
    parser = _NoteParser(base_url=url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup shouldn't kill the run
        logger.debug("HTML parse stopped early for %s", url, exc_info=True)
    title = parser.title or " ".join(parser.page_title.split())
    return Note(
        url=url,
        slug=_slug_for(url, root_path),
        title=title,
        eyebrow=parser.eyebrow,
        dek=parser.dek,
        meta=parser.meta,
        description=parser.description,
        body=parser.markdown(),
        links=parser.links,
        is_article=parser.og_type == "article",
    )


def _root_path(url: str) -> str:
    """The path prefix a crawl is confined to.

    A trailing slash is what makes the prefix check sound, so a --site given
    without one has to be repaired rather than trusted: treating "/notes" as a
    filename and taking its directory yields "/", which confines the crawl to
    nothing at all. Only a final segment that looks like a file is dropped.
    """
    path = urlparse(url).path or "/"
    if path.endswith("/"):
        return path
    head, _, last = path.rpartition("/")
    return f"{head}/" if "." in last else f"{path}/"


def _in_scope(url: str, origin: ParseResult, root_path: str) -> bool:
    """True when `url` is on the crawl's origin and at or below its root path.

    Applied to links before they are queued AND to the URL a fetch actually
    landed on: a redirect out of the section (link rot, a host migration, a
    hijacked expired route) would otherwise pull an unrelated page in and
    write it out as the candidate's own evidence.
    """
    parsed = urlparse(url)
    if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
        return False
    return parsed.path.startswith(root_path)


@dataclass
class CrawlResult:
    """What one crawl found, and whether it saw the whole site.

    `errors` exists so sync() can refuse to prune off an incomplete picture:
    a note that 503s this run is still on the site, and deleting its evidence
    over a transient failure is the one destructive mistake here.
    """

    notes: list[Note] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors


def crawl(
    site: str,
    client: httpx.Client | None = None,
    *,
    max_pages: int = MAX_PAGES,
    max_depth: int = MAX_DEPTH,
) -> CrawlResult:
    """Breadth-first over `site`, returning the article pages found.

    The crawl never leaves the origin or climbs above the starting path, so
    pointing it at a site's /notes/ section cannot wander into the rest of the
    domain. Non-article pages are still followed - collection indexes are how
    the individual notes are reached.
    """
    start = _normalize_url(site)
    origin = urlparse(start)
    root_path = _root_path(start)

    own_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers=_HEADERS)
    result = CrawlResult()
    seen = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    try:
        while queue and len(seen) <= max_pages:
            url, depth = queue.popleft()
            outcome = fetch_page(url, client=client)
            if outcome.html is None:
                logger.warning("Skipping %s: %s", url, outcome.error)
                result.errors.append(url)
                continue
            landed = _normalize_url(outcome.final_url or url)
            if landed != url:
                # Followed a redirect: re-check the destination against the
                # crawl's bounds, and dedupe on it so the canonical page is
                # not fetched again under the spelling that redirects to it.
                if not _in_scope(landed, origin, root_path):
                    logger.warning("Skipping %s: redirected out of scope to %s", url, landed)
                    continue
                if landed in seen:
                    continue
                seen.add(landed)
            if outcome.truncated:
                logger.warning(
                    "Skipping %s: page exceeded the fetch size limit and was "
                    "truncated, so it cannot be ingested whole", landed
                )
                result.errors.append(url)
                continue
            note = parse_page(outcome.html, landed, root_path)
            if note.is_article:
                if note.body:
                    result.notes.append(note)
                    logger.info("Found article: %s (%s)", note.title, note.slug)
                else:
                    logger.warning("Article at %s produced no body; skipping", landed)
            if depth >= max_depth:
                continue
            for link in note.links:
                target = _normalize_url(link)
                if target in seen or not _in_scope(target, origin, root_path):
                    continue
                seen.add(target)
                queue.append((target, depth + 1))
    finally:
        if own_client:
            client.close()
    if len(seen) > max_pages:
        logger.warning("Stopped at the %d-page crawl budget; some notes may be missing", max_pages)
        result.errors.append(f"crawl budget of {max_pages} pages exhausted")
    result.notes = _disambiguate(sorted(result.notes, key=lambda n: n.slug))
    return result


def _disambiguate(notes: list[Note]) -> list[Note]:
    """Makes slugs unique, since sync() writes one file per slug and would
    otherwise let one note silently overwrite another.

    Slugs flatten the URL path with dashes, so /a/b/ and /a-b/ collide. Rather
    than uglify every slug to rule that out, keep the readable name for the
    first note and suffix the rest.
    """
    taken: set[str] = set()
    for note in notes:
        if note.slug not in taken:
            taken.add(note.slug)
            continue
        base, suffix = note.slug, 2
        while f"{base}-{suffix}" in taken:
            suffix += 1
        logger.warning(
            "Slug collision: %s and an earlier page both flatten to %r; "
            "writing this one as %r",
            note.url, base, f"{base}-{suffix}",
        )
        note.slug = f"{base}-{suffix}"
        taken.add(note.slug)
    return notes


def sync(
    site: str,
    out_dir: Path = DEFAULT_OUT_DIR,
    *,
    prune: bool = False,
    dry_run: bool = False,
    client: httpx.Client | None = None,
) -> tuple[list[Note], list[Path]]:
    """Fetches every article under `site` into out_dir. Returns the notes
    written or refreshed, and the files pruned.

    Unchanged files are left alone rather than rewritten, so a re-sync that
    finds nothing new leaves a clean git status.
    """
    result = crawl(site, client=client)
    notes = result.notes
    if not notes:
        # A crawl that found nothing is far more likely to be a wrong URL or a
        # site that is down than a site that lost every article, so bail before
        # --prune deletes the evidence over a transient failure.
        logger.warning("No articles found at %s; leaving %s untouched", site, out_dir)
        return [], []
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for note in notes:
        path = out_dir / f"{note.slug}.md"
        rendered = note.render()
        if path.exists() and path.read_text(encoding="utf-8") == rendered:
            continue
        written.append(note)
        if not dry_run:
            path.write_text(rendered, encoding="utf-8")
    pruned = []
    if prune and not result.complete:
        # Absent from this run is not the same as gone from the site: pruning
        # off a partial crawl deletes the evidence for whichever page happened
        # to fail. Wait for a clean run.
        logger.warning(
            "Skipping --prune: %d page(s) could not be fetched this run, so "
            "an absent file cannot be told from a removed one",
            len(result.errors),
        )
    elif prune:
        keep = {f"{note.slug}.md" for note in notes} | {"README.md"}
        for path in sorted(out_dir.glob("*.md")):
            if path.name not in keep:
                pruned.append(path)
                if not dry_run:
                    path.unlink()
    return written, pruned


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Before the parser is built: --site's default is read from the environment
    # at definition time.
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        prog="python -m jobradar.evidence",
        description=(
            "Ingest published write-ups from your portfolio site into "
            "profile/evidence/ as Markdown, for the apply pipeline to use as "
            "evidence of hands-on depth."
        ),
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("JOBRADAR_EVIDENCE_SITE", ""),
        help="URL of the writing section to crawl (default: $JOBRADAR_EVIDENCE_SITE)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete evidence files that no longer exist on the site",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without touching the filesystem",
    )
    args = parser.parse_args(argv)

    if not args.site:
        parser.error(
            "no site to crawl: pass --site URL or set JOBRADAR_EVIDENCE_SITE in .env"
        )

    written, pruned = sync(args.site, args.out, prune=args.prune, dry_run=args.dry_run)
    verb = "Would write" if args.dry_run else "Wrote"
    if written:
        for note in written:
            logger.info("%s %s.md - %s", verb, note.slug, note.title)
    else:
        logger.info("No changes: every article on the site is already ingested")
    for path in pruned:
        logger.info("%s %s", "Would remove" if args.dry_run else "Removed", path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

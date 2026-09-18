"""Turn Word CVs (.docx) in profile/cvs/ into the
Markdown files the tool reads, automatically, whenever the CVs are read (see
matching.load_profile and the doctor). There is no command to run: a user who
keeps their CV in Word drops the file in and carries on updating it in Word.

Why a converter of its own rather than a library: a .docx is a zip of XML,
and a CV uses a small part of it - headings, bullets, bold, links, and tables
or text boxes for layout. Reading that part with the standard library keeps
the install free of a Word dependency, the same trade-off evidence.py makes
for HTML. Anything outside it (images, shapes, footnotes) is dropped.

Why no model call: the base CV is what every posting is scored against and
what tailoring starts from, so its text must be the user's own, word for
word. A deterministic conversion cannot invent a line. What it cannot do is
lay out a two-column design: tables and text boxes come out in reading order,
one cell after another, which is why the user is asked for a read-through.

Legacy .doc files (Word 97-2003) are binary, not XML, and reading them
would take LibreOffice, which the people this is for rarely have installed.
They are recognised rather than read: the user is asked to re-save the file
as .docx, which any current word processor does, instead of the file being
ignored in silence.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


WORD_SUFFIXES = (".docx", ".doc")

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Characters people type (or Word autoformats) as a bullet instead of using a
# real list. Hyphens are left out: "- " already reads as a Markdown bullet.
_MANUAL_BULLET = re.compile(r"^[•▪▫◦●○■□►▶➢➤✓✔·–—]\s*")

# Wrappers whose children are ordinary content: content controls (Word
# templates are full of them), tracked insertions, smart tags.
_TRANSPARENT = {f"{_W}sdt", f"{_W}sdtContent", f"{_W}ins", f"{_W}smartTag",
                f"{_W}customXml", f"{_W}fldSimple", f"{_W}moveTo"}


class ConversionError(Exception):
    """A file that could not be read as a CV; the message says what to do."""


@dataclass
class _Styles:
    """What a paragraph's style says about it, resolved through basedOn."""

    names: dict[str, str]
    based_on: dict[str, str]
    outline: dict[str, int]
    listed: set[str]

    def _chain(self, style_id: str | None):
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            yield style_id
            style_id = self.based_on.get(style_id)

    def heading_level(self, style_id: str | None) -> int | None:
        """0 for a Title, 1-6 for Heading 1-6, None for body text."""
        for sid in self._chain(style_id):
            name = self.names.get(sid, "").lower()
            if name == "title":
                return 0
            m = re.fullmatch(r"heading (\d)", name)
            if m:
                return int(m.group(1))
            if sid in self.outline:
                return self.outline[sid] + 1
        return None

    def is_list(self, style_id: str | None) -> bool:
        return any(sid in self.listed for sid in self._chain(style_id))


def _read_styles(xml: bytes | None) -> _Styles:
    styles = _Styles({}, {}, {}, set())
    if not xml:
        return styles
    for style in ET.fromstring(xml).iter(f"{_W}style"):
        sid = style.get(f"{_W}styleId")
        if not sid:
            continue
        name = style.find(f"{_W}name")
        if name is not None:
            # Built-in styles keep their English name here whatever the UI
            # language ("heading 1", not "Überschrift 1").
            styles.names[sid] = name.get(f"{_W}val", "")
        based = style.find(f"{_W}basedOn")
        if based is not None:
            styles.based_on[sid] = based.get(f"{_W}val", "")
        ppr = style.find(f"{_W}pPr")
        if ppr is not None:
            outline = ppr.find(f"{_W}outlineLvl")
            if outline is not None and outline.get(f"{_W}val", "").isdigit():
                level = int(outline.get(f"{_W}val"))
                if level < 9:  # 9 is Word's "body text"
                    styles.outline[sid] = level
            if _num_id(ppr):
                styles.listed.add(sid)
    return styles


def _read_links(xml: bytes | None) -> dict[str, str]:
    if not xml:
        return {}
    return {
        rel.get("Id", ""): rel.get("Target", "")
        for rel in ET.fromstring(xml).iter(f"{_REL}Relationship")
        if rel.get("TargetMode") == "External"
    }


def _num_id(ppr: ET.Element) -> bool:
    """Whether a paragraph-properties element puts the paragraph in a list.
    numId 0 is Word's way of switching an inherited list off."""
    num = ppr.find(f"{_W}numPr")
    if num is None:
        return False
    num_id = num.find(f"{_W}numId")
    return num_id is None or num_id.get(f"{_W}val") != "0"


def _is_on(rpr: ET.Element | None, tag: str) -> bool:
    if rpr is None:
        return False
    el = rpr.find(f"{_W}{tag}")
    return el is not None and el.get(f"{_W}val", "true") not in ("0", "false", "none")


class _Converter:
    def __init__(self, styles: _Styles, links: dict[str, str]):
        self.styles = styles
        self.links = links

    # --- blocks -----------------------------------------------------------

    def blocks(self, container: ET.Element) -> list[str]:
        """Markdown blocks for a body, cell, text box or header, in order."""
        out: list[str] = []
        for child in container:
            if child.tag == f"{_W}p":
                out.extend(self.paragraph(child))
            elif child.tag == f"{_W}tbl":
                # Layout tables read cell by cell, row by row: a CV's
                # "2022-2026 | Senior PM at Acme" row stays together in order.
                for row in child.findall(f"{_W}tr"):
                    for cell in row.findall(f"{_W}tc"):
                        out.extend(self.blocks(cell))
            elif child.tag in _TRANSPARENT:
                out.extend(self.blocks(child))
        return out

    def paragraph(self, p: ET.Element) -> list[str]:
        ppr = p.find(f"{_W}pPr")
        style_id = None
        if ppr is not None:
            style = ppr.find(f"{_W}pStyle")
            if style is not None:
                style_id = style.get(f"{_W}val")
        level = self.styles.heading_level(style_id)
        if ppr is not None:
            outline = ppr.find(f"{_W}outlineLvl")
            if outline is not None and outline.get(f"{_W}val", "").isdigit() and int(outline.get(f"{_W}val")) < 9:
                level = int(outline.get(f"{_W}val")) + 1

        boxes: list[str] = []
        pieces = self.inline(p, boxes, plain=level is not None)
        text = _join_pieces(pieces).strip()

        out: list[str] = []
        if text:
            if level is not None:
                out.append("#" * min(level + 1, 6) + " " + " ".join(text.split()))
            elif self._is_list(ppr, style_id):
                depth = 0
                if ppr is not None:
                    ilvl = ppr.find(f"{_W}numPr/{_W}ilvl")
                    if ilvl is not None and ilvl.get(f"{_W}val", "").isdigit():
                        depth = int(ilvl.get(f"{_W}val"))
                out.append("  " * depth + "- " + _MANUAL_BULLET.sub("", text).replace("\n", "\n  " + "  " * depth))
            elif _MANUAL_BULLET.match(text):
                out.append("- " + _MANUAL_BULLET.sub("", text).replace("\n", "\n  "))
            else:
                out.append(text)
        # A text box anchored in this paragraph is read after it.
        return out + boxes

    def _is_list(self, ppr: ET.Element | None, style_id: str | None) -> bool:
        # The paragraph's own numbering wins over its style's, including a
        # numId of 0 that switches the style's list off.
        if ppr is not None and ppr.find(f"{_W}numPr") is not None:
            return _num_id(ppr)
        return self.styles.is_list(style_id)

    # --- inline -----------------------------------------------------------

    def inline(self, el: ET.Element, boxes: list[str], plain: bool, link: str | None = None) -> list:
        """(text, bold, italic, link) pieces; `boxes` collects text boxes."""
        pieces: list = []
        in_field_code = False
        for child in el:
            tag = child.tag
            if tag == f"{_W}r":
                rpr = child.find(f"{_W}rPr")
                bold = not plain and _is_on(rpr, "b")
                italic = not plain and _is_on(rpr, "i")
                for part in child:
                    ptag = part.tag
                    if ptag == f"{_W}fldChar":
                        kind = part.get(f"{_W}fldCharType")
                        in_field_code = kind == "begin"
                    elif in_field_code:
                        continue  # field instructions (PAGE, HYPERLINK "...")
                    elif ptag == f"{_W}t":
                        pieces.append((part.text or "", bold, italic, link))
                    elif ptag == f"{_W}tab":
                        pieces.append((" ", False, False, None))
                    elif ptag in (f"{_W}br", f"{_W}cr"):
                        if part.get(f"{_W}type") != "page":
                            pieces.append(("\n", False, False, None))
                    elif ptag == f"{_W}noBreakHyphen":
                        pieces.append(("-", bold, italic, link))
                    else:
                        self._collect_boxes(part, boxes)
            elif tag == f"{_W}hyperlink":
                target = self.links.get(child.get(f"{_R}id", ""))
                pieces.extend(self.inline(child, boxes, plain, target or link))
            elif tag in _TRANSPARENT:
                pieces.extend(self.inline(child, boxes, plain, link))
            elif tag == f"{_MC}AlternateContent":
                self._collect_boxes(child, boxes)
        return pieces

    def _collect_boxes(self, el: ET.Element, boxes: list[str]) -> None:
        # Word writes a text box twice, as DrawingML (mc:Choice) and as a VML
        # fallback; reading both would print it twice.
        if el.tag == f"{_MC}AlternateContent":
            choice = el.find(f"{_MC}Choice")
            el = choice if choice is not None else el.find(f"{_MC}Fallback")
            if el is None:
                return
        for box in el.iter(f"{_W}txbxContent"):
            boxes.extend(self.blocks(box))


def _join_pieces(pieces: list) -> str:
    """Merge runs with the same formatting (Word splits text into runs at
    every edit), then mark bold, italic and links up once per stretch."""
    merged: list[list] = []
    for text, bold, italic, link in pieces:
        if merged and merged[-1][1:] == [bold, italic, link]:
            merged[-1][0] += text
        else:
            merged.append([text, bold, italic, link])
    out = []
    for text, bold, italic, link in merged:
        # Markers must hug the text: "**Acme **" is not bold in Markdown.
        core = text.strip(" ")
        if not core:
            out.append(text)
            continue
        lead = text[: len(text) - len(text.lstrip(" "))]
        trail = text[len(text.rstrip(" ")):]
        if link:
            shown = link.removeprefix("mailto:")
            if core not in (link, shown):
                core = f"[{core}]({link})"
        mark = "***" if bold and italic else "**" if bold else "*" if italic else ""
        if mark:
            core = "\n".join(f"{mark}{line.strip()}{mark}" if line.strip() else line for line in core.split("\n"))
        out.append(lead + core + trail)
    return "\n".join(line.strip() for line in "".join(out).split("\n"))


def docx_to_markdown(data: bytes) -> str:
    """The Markdown for a .docx file's bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                raise ConversionError("not a Word document (no word/document.xml inside)")
            document = ET.fromstring(zf.read("word/document.xml"))
            styles = _read_styles(zf.read("word/styles.xml") if "word/styles.xml" in names else None)
            rels = "word/_rels/document.xml.rels"
            converter = _Converter(styles, _read_links(zf.read(rels) if rels in names else None))

            # Contact details often live in the page header of a CV template.
            # Several headers (first page, odd, even) usually repeat each
            # other, so each distinct block is kept once, before the body.
            header_blocks: list[str] = []
            for name in sorted(n for n in names if re.fullmatch(r"word/header\d*\.xml", n)):
                for block in converter.blocks(ET.fromstring(zf.read(name))):
                    if block not in header_blocks:
                        header_blocks.append(block)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ConversionError(f"could not be read as .docx ({exc})") from exc

    body = document.find(f"{_W}body")
    blocks = converter.blocks(body) if body is not None else []
    # Name first, then the contact line, as in the CV template.
    at = 1 if blocks and blocks[0].startswith("# ") else 0
    return _assemble(blocks[:at] + header_blocks + blocks[at:])


def _assemble(blocks: list[str]) -> str:
    """Blank line between blocks, except between items of one list."""
    out: list[str] = []
    for block in blocks:
        if out:
            both_items = block.lstrip().startswith("- ") and out[-1].lstrip().startswith("- ")
            out.append("\n" if both_items else "\n\n")
        out.append(block)
    return "".join(out).strip() + "\n"


def _word_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".doc":
        raise ConversionError(
            "is in the old Word format (.doc). Open it in Word, Pages or "
            "LibreOffice, save it as .docx, and put that file here instead"
        )
    return path.read_bytes()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _text_digest(text: str) -> str:
    # Line endings and trailing blank lines are not edits: git on Windows and
    # most editors change them without the user doing anything.
    return _digest(text.replace("\r\n", "\n").strip().encode("utf-8"))


# First line of every Markdown CV made from a Word file. `source` is the Word
# file as it was converted, `text` is what was written below this line: when
# the Word file changes and the text is still untouched, the CV is remade;
# once someone edits the text, it is theirs and is left alone. An HTML comment
# never shows in a rendered PDF, and the model reads straight past it.
_HEADER = (
    "<!-- Made by jobradar from {name}, and remade whenever that file changes, "
    "until you edit this file yourself. source={source} text={text} -->"
)
_HEADER_RE = re.compile(r"<!-- Made by jobradar from .*? source=(\w+) text=(\w+) -->\s*")


@dataclass
class Outcome:
    """What happened to one Word file in profile/cvs/."""

    word: Path
    status: str  # "made" | "remade" | "kept_edits" | "failed"
    detail: str = ""

    @property
    def message(self) -> str:
        md = self.word.with_suffix(".md").name
        if self.status == "made":
            return f"made {md} from {self.word.name} — read it through once"
        if self.status == "remade":
            return f"remade {md} from the updated {self.word.name}"
        if self.status == "kept_edits":
            return (
                f"{self.word.name} changed, but {md} has your own edits, so it was "
                f"kept; delete {md} to make it again from the Word file"
            )
        return f"{self.word.name} {self.detail}"


def sync_word_cvs(cvs_dir: Path) -> list[Outcome]:
    """Brings the Markdown CVs in line with the Word files beside them.

    A Word file with no .md gets one. A .md this function made earlier is
    remade when its Word file changes, unless it has been edited since. A .md
    it did not make (no header) is the user's own and is never touched. Cheap
    enough to run on every read of the CVs: an unchanged Word file is only
    hashed, never converted.
    """
    outcomes: list[Outcome] = []
    stems: set[str] = set()
    # cv.docx beside cv.doc: the .docx is the newer save, and the only one
    # that can be read, so it is the one that makes cv.md.
    for word in sorted(word_files(cvs_dir), key=lambda p: (p.stem, p.suffix.lower() != ".docx")):
        if word.stem in stems:
            continue
        stems.add(word.stem)
        md = word.with_suffix(".md")
        source = _digest(word.read_bytes())
        existed = md.exists()
        if existed:
            current = md.read_text(encoding="utf-8")
            m = _HEADER_RE.match(current)
            if m is None:
                continue
            if m.group(1) == source:
                continue
            if m.group(2) != _text_digest(current[m.end():]):
                outcomes.append(Outcome(word, "kept_edits"))
                continue
        try:
            text = docx_to_markdown(_word_bytes(word))
        except (ConversionError, OSError) as exc:
            outcomes.append(Outcome(word, "failed", str(exc)))
            continue
        if not text.strip():
            outcomes.append(Outcome(word, "failed", "has no text that could be read (a scanned image?)"))
            continue
        header = _HEADER.format(name=word.name, source=source, text=_text_digest(text))
        md.write_text(f"{header}\n\n{text}", encoding="utf-8")
        outcomes.append(Outcome(word, "remade" if existed else "made"))
    return outcomes


def word_files(directory: Path) -> list[Path]:
    """Word files in a directory, leaving out Word's "~$" lock files."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.suffix.lower() in WORD_SUFFIXES and not p.name.startswith("~$")
    )

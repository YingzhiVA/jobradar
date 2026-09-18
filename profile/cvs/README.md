# CVs

Put one CV per role category in this directory, as plain Markdown or text
(not PDF — the matching step feeds these directly to the model as text, so
skip the PDF-parsing step entirely and just paste/format your CV content as
Markdown, or convert a Word file: see [From Word](#from-word)). The
**filename** (without extension) is used as the CV's label in match output,
so name it after the role category, e.g.:

```
product-manager.md
program-manager.md
technical-pm.md
```

Delete `example.md` once you've added your real CV(s) — it's only here to
document the expected format.

If you only have one CV, just add the one file; the tool works fine with a
single CV (every posting is simply scored against it).

## From Word

If your CV is a Word file, just put the `.docx` here. The setup check
(`python -m jobradar.doctor`) and every run after it make a Markdown copy of
the same name beside it, `my-cv.docx` becoming `my-cv.md`, which is what the
tool reads. Keep working on your CV in Word if you like: when the Word file
changes, the Markdown copy is made again on the next run.

The copy keeps your text word for word, with headings, bullets, bold and
links. Read it through once: tables and text boxes, which Word CV templates
use for two-column layouts, come out one cell after another, and images are
dropped. You can edit the copy too. Once you do, it is yours and is never
overwritten; if the Word file changes after that, the setup check tells you,
and deleting the `.md` makes a fresh copy.

An old-format `.doc` (Word 2003 and earlier) can't be read. The setup check
will tell you if it finds one: open it in Word, Pages or LibreOffice, save
it as `.docx`, and put that here instead.

"""Write the application documents: a tailored CV plus, when needed, a cover
letter, in ONE Sonnet call per posting. This is where writing quality
matters, so it runs on Sonnet (same model split rationale as writeup.py).

Why one call instead of one per document: structured-output schemas are part
of the request's cached prefix, so two calls with different output schemas
can't share a prompt cache even when their system blocks are byte-identical
(observed: both calls paid the full ~10k-token cache write). One call with
one stable schema halves the Sonnet round-trips AND lets the cached
candidate-profile block be reused across every posting in a batch.

Cache layout per call: system = [profile block (stable all run, cached),
job-description block (per posting, cached)], task instructions in the user
message after the cached prefix.

Non-English postings get one extra Sonnet call (translate_cv) that renders
the tailored CV in the posting's language. It reuses the same cached system
prefix, so it is a cache-read on top of the tailoring call. The cover letter
needs no translation: write_application_docs already writes it in the
posting's language.

Both calls stream. Nothing here needs the tokens early - the documents are
written to disk whole - but a 12k-token CV plus cover letter takes minutes,
and a non-streaming request that long is two problems: it can hit the SDK's
HTTP read timeout, and it leaves the caller with nothing to show for the wait.
Streaming fixes both; `on_delta` is what apply/progress.py feeds its live
counter from.
"""

from __future__ import annotations

import logging
from typing import Callable

import anthropic
from pydantic import BaseModel, Field

from .. import usage
from ..llm import MODEL_DEFAULTS, model_for
from .util import drain_stream

logger = logging.getLogger(__name__)

# Overridable via JOBRADAR_APPLY_WRITER_MODEL, resolved per call; see jobradar.llm.
WRITER_MODEL = MODEL_DEFAULTS["JOBRADAR_APPLY_WRITER_MODEL"]

# ISO 639-1 code -> English name, for readable prompts. Covers the languages a
# Switzerland-focused search actually turns up; unknown codes fall through to
# the code itself (which the model still understands).
LANGUAGE_NAMES = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "nl": "Dutch",
    "pt": "Portuguese",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get((code or "").lower(), code or "the posting's language")


# Swiss German and Swiss French are not the German of Germany and the French
# of France, and every posting this pipeline sees is Swiss. A reviewer in
# Zurich reads a stray ß-rule violation the way a reviewer in Berlin would
# read "colour": not wrong exactly, just written by someone from elsewhere.
_SWISS_CONVENTIONS = {
    "de": """\
Write Swiss Standard German (Schweizer Hochdeutsch), not the German of
Germany:
- The letter ß does not exist in Swiss orthography. Always write ss:
  Strasse, grösste, Mass, dass. Never use it, not even in quotations.
- Quote with guillemets «so», not with German quotation marks.
- Group thousands with an apostrophe: CHF 120'000, not 120.000.
- Swiss business vocabulary where it differs: Lohn (not Gehalt),
  Stelleninserat (not Stellenanzeige), Mitarbeitende. Dates as 06.08.2026.""",
    "fr": """\
Write Swiss French (français de Suisse romande), not the French of France:
- septante for 70 and nonante for 90, never soixante-dix or quatre-vingt-dix.
  Leave 80 as quatre-vingts unless the posting itself writes huitante.
- Quote with guillemets « so », a space inside each mark.
- Group thousands with an apostrophe: CHF 120'000.
- Swiss vocabulary where it differs: place de travail, formation continue.
  Dates as 06.08.2026.""",
}

# Used when the language is only known to the model, not to the caller.
SWISS_CONVENTIONS_ANY = """\
Every posting here is Swiss. If that language is German or French, write the
Swiss variety: Swiss Standard German has no ß (write ss - Strasse, grösste,
dass), Swiss French uses septante and nonante, and both quote with guillemets
and group thousands with an apostrophe (CHF 120'000)."""


def swiss_conventions(code: str) -> str:
    """Prompt text for writing the Swiss variety of `code`'s language, or ""
    for a language with no Swiss variety to get wrong (English included)."""
    return _SWISS_CONVENTIONS.get((code or "").lower(), "")


def swiss_spelling(text: str, code: str) -> str:
    """Deterministic backstop for the one Swiss rule a model trained mostly on
    German German keeps breaking. The prompt makes it likely; this makes it
    certain. It also Swiss-ifies a German name carrying an ß, which is the
    trade Swiss usage itself makes (Swiss papers write Straßburg as
    Strassburg)."""
    if not (code or "").lower().startswith("de"):
        return text
    return text.replace("ß", "ss").replace("ẞ", "SS")


def _system_blocks(profile_block: str, jd_block: str) -> list[dict]:
    """The two cached system blocks shared by every writing call for one
    posting: the run-stable candidate profile, then the posting-specific JD.
    Identical bytes across the tailoring and translation calls, so the second
    call reads the cache the first one wrote."""
    return [
        {"type": "text", "text": profile_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": jd_block, "cache_control": {"type": "ephemeral"}},
    ]


_STYLE_RULES = """\
Style rules for every document you write:
- Never invent employers, job titles, dates, degrees, certificates, tools,
  or metrics that are not in the candidate profile. Tailoring means
  reordering, rephrasing, emphasizing, and trimming - never fabricating.
- The tailored CV must stay traceable to the base CV. The candidate's
  interview stories and published write-ups (if the profile includes them)
  are real facts too: use them freely in the cover letter, and in the CV only
  to sharpen the phrasing of a bullet that already exists there, never to add
  new bullets.
- Respect what a published write-up withheld. Where one says its details are
  generalized, do not name the employer or client it left out, and do not
  invent a figure it chose not to give. It was published that way on purpose.
- No em-dashes. Use commas, colons, or parentheses instead.
- No generic filler ("passionate", "dynamic", "proven track record") unless
  backed by a concrete fact in the same sentence.
"""


def build_jd_block(
    jd_markdown: str,
    url: str,
    retrieved_on: str,
    prior_analysis: str | None = None,
) -> str:
    """The second cached system block: everything posting-specific the
    writing call needs."""
    parts = [
        f"# Job posting\n\nRetrieved {retrieved_on} from {url}\n\n{jd_markdown.strip()}",
    ]
    if prior_analysis:
        parts.append(f"# Prior fit analysis\n\n{prior_analysis.strip()}")
    return "\n\n".join(parts)


class ApplicationDocs(BaseModel):
    tailored_cv_markdown: str
    # Empty string when no cover letter was requested for this posting. The
    # field always exists so the output schema (part of the cached prefix)
    # stays identical across postings and tiers.
    cover_letter_markdown: str = ""
    # What was emphasized/reordered and why - goes into notes.md so the user
    # can review the tailoring decisions without diffing the CVs.
    highlights: list[str] = Field(default_factory=list)
    # Which base-CV bullets were deleted for irrelevance, and why. The only
    # part of tailoring that leaves no trace in the document itself, which is
    # exactly why it needs a field: asked only to "drop irrelevant bullets" in
    # passing, the model reliably reported reordering and rephrasing while
    # keeping every bullet, because nothing made it commit to a cut. Having to
    # name what it removed forces the judgement to actually happen, and gives
    # the user a reviewable list to put a bullet back from.
    dropped: list[str] = Field(default_factory=list)
    # Honest gaps vs. the posting's requirements the user should go in aware of.
    gaps: list[str] = Field(default_factory=list)
    # Things only the user can decide or verify before submitting.
    open_questions: list[str] = Field(default_factory=list)


_TAILOR_PROMPT = """\
Prepare application documents for the job posting in the system prompt,
based on the candidate's CV "{base_cv}" (also in the system prompt).

{style_rules}
1. Tailored CV (tailored_cv_markdown):
- Write it in the language of the base CV below, whatever language the posting
  is in. Only the cover letter follows the posting's language. A version of
  this CV in the posting's language is produced by a separate call afterwards,
  from the CV you return here, so returning it already translated destroys the
  original rather than saving a step.
- Cut first, then order what is left. Take the base CV one bullet at a time
  and ask a single question of each: does this bullet help someone hiring for
  THIS posting decide to interview the candidate? If the answer is no, delete
  the bullet. Do not tighten it, do not soften it, do not move it to the
  bottom of its role - a bullet left anywhere on the page still spends the
  reviewer's attention and the space a relevant bullet needed.
- Judge relevance, never quality. Every bullet in the base CV is well written
  and true; that is why it is in there. A strong bullet about work this
  posting does not care about is exactly the bullet to cut, and cutting it
  will feel wrong every time. Operating cadence and team rituals on a
  hands-on engineering posting, deep achievements from an industry this role
  has nothing to do with, tooling the role never touches: these go, however
  good they sound. If you reach the end having kept nearly everything, you
  judged quality instead of relevance - go back through it.
- What is never deleted: contact details, the professional summary, and the
  list of employers with their titles and dates. Cut bullets within a role,
  never a role itself - an omitted employer reads as an employment gap, which
  costs far more than a weak bullet. Leave every role at least one bullet and
  the two most recent roles at least two, so no position looks abandoned.
  Education, certifications, and languages stay.
- Reorder what survives so the most posting-relevant achievement leads each
  role and each section.
- Rephrase surviving bullets to use the posting's own terminology where the
  underlying experience genuinely matches (this is what recruiters and ATS
  scans look for), but keep each claim traceable to the base CV.
- Adjust the professional summary to speak directly to this role's core
  requirements.
- Output the COMPLETE tailored CV as markdown, including contact details and
  every section that survived, ready to be exported to PDF.

2. Cover letter (cover_letter_markdown): {cover_letter_instruction}

3. Also report, briefly and concretely:
- dropped: every base-CV bullet you deleted, each as a short quote of it
  followed by why it is not relevant to this posting. This is a record of
  what you actually cut, not a plan for what could be cut. An empty list
  says the CV you returned is the base CV reordered, which is a tailoring
  failure unless this posting genuinely calls on every bullet in it.
- highlights: the tailoring decisions you made and why (max 6).
- gaps: requirements of the posting the candidate does not clearly meet (be
  honest; empty list only if there are truly none).
- open_questions: anything only the candidate can decide or verify before
  submitting (max 4; empty if none).
"""

_COVER_LETTER_INSTRUCTION = """\
Write one, in {language} (the posting's language). This is the only document
in this call written in that language; the CV above stays in the base CV's.
- Maximum ~300 words of body text: three or four short paragraphs, one page.
- Structure: why this role and company specifically (one concrete hook from
  the posting), then the two or three most relevant achievements mapped to
  the role's core requirements, then a short confident close.
- Ground every claim in the candidate profile. If the profile includes
  interview stories, prefer them as the source of achievements: they carry
  the situation and measurable result that make a claim credible, where a CV
  bullet alone reads as assertion. Address the posting's stated must-haves,
  not generic virtues.
- Where the posting asks for hands-on technical depth and a published
  write-up demonstrates it, say so in one clause and cite its source URL
  inline. A reader can check that; an assertion they cannot check is worth
  less. At most one such link, and only when the write-up is genuinely on the
  posting's topic - a link to something tangential reads as padding.
- Use "[Hiring manager name]" as a placeholder only if the posting names no
  contact person. Do not invent addresses or dates.
- Output the complete letter as markdown."""

_NO_COVER_LETTER_INSTRUCTION = (
    "Not needed for this posting: return an empty string here."
)


def write_application_docs(
    base_cv: str,
    language: str,
    include_cover_letter: bool,
    profile_block: str,
    jd_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> ApplicationDocs | None:
    model = model or model_for("JOBRADAR_APPLY_WRITER_MODEL")
    cover_letter_instruction = (
        "\n\n".join(
            part
            for part in (
                _COVER_LETTER_INSTRUCTION.format(language=language_name(language)),
                swiss_conventions(language),
            )
            if part
        )
        if include_cover_letter
        else _NO_COVER_LETTER_INSTRUCTION
    )
    try:
        with client.messages.stream(
            model=model,
            # A full CV plus a cover letter plus JSON overhead; too small a
            # cap truncates the CV mid-section (same lesson as writeup.py's
            # max_tokens comment).
            max_tokens=12288,
            system=_system_blocks(profile_block, jd_block),
            messages=[
                {
                    "role": "user",
                    "content": _TAILOR_PROMPT.format(
                        base_cv=base_cv,
                        style_rules=_STYLE_RULES,
                        cover_letter_instruction=cover_letter_instruction,
                    ),
                }
            ],
            output_format=ApplicationDocs,
        ) as stream:
            drain_stream(stream, on_delta)
            response = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - per-posting best effort
        logger.warning("Application writing failed: %s", exc)
        return None
    if usage_acc is not None:
        usage.accumulate(usage_acc, response)
    if response.stop_reason == "max_tokens":
        logger.warning("Application writing hit max_tokens; output may be truncated")
    docs = response.parsed_output
    if docs is None:
        # Truncated or otherwise unparseable JSON: no documents to write, and
        # the caller leaves the entry queued for a re-run.
        logger.warning("Application writing returned no parseable documents")
        return None
    # The cover letter is the only half written in the posting's language; the
    # tailored CV here is still the English original.
    docs.cover_letter_markdown = swiss_spelling(docs.cover_letter_markdown, language)
    return docs


class TranslatedCV(BaseModel):
    translated_cv_markdown: str


_TRANSLATE_CV_PROMPT = """\
Translate the tailored CV below into {language_name}, for submission to the
job posting in the system prompt. (The cover letter, if any, was already
written in {language_name}; only the CV needs translating.)

{style_rules}
{swiss_conventions}
Translation rules:
- Preserve the markdown structure exactly: the same headings, bullets,
  ordering, and layout. Only the language of the prose changes.
- Keep proper nouns unchanged: company names, product names, technologies,
  tools, and the names of certifications and degrees. Translate generic role
  descriptions, responsibilities, and everyday nouns.
- Use natural, professional {language_name} business-CV phrasing that a
  native reviewer would expect, not a word-for-word gloss.
- Do not add, drop, soften, or embellish any fact. This is a translation of
  the tailored CV, not a re-tailoring.
- Output the COMPLETE translated CV as markdown.

Tailored CV to translate:
{cv_markdown}
"""


def translate_cv(
    cv_markdown: str,
    language: str,
    profile_block: str,
    jd_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> str | None:
    """Translate an already-tailored CV into `language` (an ISO 639-1 code or a
    readable name). Reuses the posting's cached system prefix so the call is a
    cheap cache-read on top of the tailoring call. Returns the translated
    markdown, or None on failure (caller keeps the English CV)."""
    model = model or model_for("JOBRADAR_APPLY_WRITER_MODEL")
    try:
        with client.messages.stream(
            model=model,
            max_tokens=8192,
            system=_system_blocks(profile_block, jd_block),
            messages=[
                {
                    "role": "user",
                    "content": _TRANSLATE_CV_PROMPT.format(
                        language_name=language_name(language),
                        style_rules=_STYLE_RULES,
                        swiss_conventions=swiss_conventions(language),
                        cv_markdown=cv_markdown,
                    ),
                }
            ],
            output_format=TranslatedCV,
        ) as stream:
            drain_stream(stream, on_delta)
            response = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - per-posting best effort
        logger.warning("CV translation failed: %s", exc)
        return None
    if usage_acc is not None:
        usage.accumulate(usage_acc, response)
    if response.stop_reason == "max_tokens":
        logger.warning("CV translation hit max_tokens; output may be truncated")
    if response.parsed_output is None:
        return None
    return swiss_spelling(response.parsed_output.translated_cv_markdown, language)

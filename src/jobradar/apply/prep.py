"""Interview-prep sheet for a drafted or submitted application: one Sonnet
call that maps the posting's requirements and likely interview questions onto
the candidate's prepared material, and flags what none of it covers yet.

Two kinds of prepared material, and the split is the point. The STAR stories
in profile/stories/ are behavioural - managing up, conflict, ambiguity. The
write-ups in profile/evidence/ are technical and public. Before they were
ingested, every "walk me through something you built" question landed in
coverage_gaps pointing at a CV bullet, because no prepared story answered a
technical question. Both are now candidates for recommended_material.

Runs on demand (python -m jobradar.apply --prep FOLDER), after the invite or
while deciding where to spend prep time - not as part of drafting, since most
drafted applications never reach an interview.

Reuses the same two cached system blocks as tailor.py (candidate profile +
job description), so running --prep right after drafting is a cache-read on
top of the tailoring call.

Written in the posting's language, like the cover letter: a German posting
means a German interview, and rehearsing answers in one language for an
interview held in another is the wrong preparation.
"""

from __future__ import annotations

import logging
from typing import Callable

import anthropic
from pydantic import BaseModel, Field

from .. import usage
from ..llm import MODEL_DEFAULTS, model_for
from .tailor import (
    SWISS_CONVENTIONS_ANY,
    _system_blocks,
    language_name,
    swiss_conventions,
    swiss_spelling,
)
from .util import drain_stream

logger = logging.getLogger(__name__)

# Overridable via JOBRADAR_APPLY_PREP_MODEL, resolved per call; see jobradar.llm.
PREP_MODEL = MODEL_DEFAULTS["JOBRADAR_APPLY_PREP_MODEL"]


class PrepQuestion(BaseModel):
    question: str
    # Label of the prepared story OR published write-up to lead with (as used
    # in the profile), or "" when nothing prepared fits - the angle then says
    # what to build the answer from.
    recommended_material: str = ""
    angle: str


class PrepSheet(BaseModel):
    # ISO 639-1 code of the language the sheet is written in, which is the
    # posting's. Reported back rather than assumed, so render_prep_md can put
    # the section headings in the same language as what sits under them.
    language: str = "en"
    # Two- or three-sentence "tell me about yourself" positioning for this role.
    positioning: str
    likely_questions: list[PrepQuestion] = Field(default_factory=list)
    # Requirements or likely questions nothing prepared covers well, each with
    # a pointer to the CV experience a new story could be drafted from.
    coverage_gaps: list[str] = Field(default_factory=list)
    # Published write-ups likely to come up, as "label - why", so the candidate
    # can re-read them beforehand. A linked write-up may well have been read by
    # the interviewer; being vaguer about it than they are is a bad look.
    write_ups_to_revisit: list[str] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)


_PREP_PROMPT = """\
Prepare the candidate for an interview for the job posting in the system
prompt, using their profile (CVs, identity statement, and - if present -
prepared interview stories in STAR form and published technical write-ups).

{language_instruction} Everything the candidate will read or say - the
positioning, the questions, the angles, the gaps, the questions to ask - goes
in that language, because that is the language the interview will be held in.
Two things stay verbatim regardless: the labels of prepared stories and
write-ups (they name files in the profile), and quotes lifted from the
posting. Also report that language as an ISO 639-1 code in the language
field.

Produce:

1. positioning: a two- or three-sentence "tell me about yourself" framing
   tailored to THIS role - the through-line from the candidate's background to
   this job, in first person, concrete, no filler.

2. likely_questions (8-12): the questions this specific interview is likely to
   open with. Mix behavioral questions ("tell me about a time...") derived
   from the posting's stated requirements, role- and company-specific ones,
   and - where the posting asks for hands-on technical depth - the technical
   questions that depth will actually be probed with ("walk me through
   something you built", "how would you evaluate that", "how did you know it
   was working"). For each:
   - recommended_material: the prepared story OR published write-up to lead
     with, named by its label in the profile. Leave empty if nothing prepared
     fits.
   - angle: which part of it to emphasize for this posting's framing, or -
     when nothing fits - what CV experience to build the answer from.
   Match the kind of material to the kind of question: a behavioral question
   wants a STAR story, a technical one usually wants a write-up. Spread
   recommendations across the available material; if one item is the best lead
   for many questions, say so in its angle rather than hiding the imbalance.

3. coverage_gaps: requirements or likely questions where nothing prepared is
   convincing. For each, name the gap and the CV experience a new STAR story
   could be drafted from (or say plainly that it needs an honest "I haven't
   done this, here's the closest I've come" answer).

4. write_ups_to_revisit: the published write-ups most likely to come up for
   THIS role, each as "label - why it will come up". Include one only when the
   posting makes its topic genuinely relevant; an empty list is the right
   answer for a role the write-ups don't touch. Remember these are public: an
   interviewer may have read one, so the candidate should not be vaguer about
   their own work than the reader is.

5. questions_to_ask (3-5): sharp questions for the interviewer that show the
   candidate read the posting - about the role's mandate, team, or success
   criteria. Nothing answerable by the posting itself.

Be blunt where it helps: a gap named before the interview is preparation, a
gap discovered during it is a rejection.
"""

_LANGUAGE_INSTRUCTION = "Write the sheet in {language}, the posting's language."
_DETECT_LANGUAGE_INSTRUCTION = (
    "Write the sheet in the language the posting itself is written in - not "
    "the language of the candidate's profile, which is a separate question."
)


def generate_prep(
    profile_block: str,
    jd_block: str,
    client: anthropic.Anthropic,
    model: str | None = None,
    usage_acc: dict | None = None,
    language: str | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> PrepSheet | None:
    """language is the posting's ISO 639-1 code when the tracker recorded one
    at drafting time; None lets the model read it off the posting, which is
    what happens for a folder that was never tracked."""
    model = model or model_for("JOBRADAR_APPLY_PREP_MODEL")
    instruction = "\n\n".join(
        part
        for part in (
            (
                _LANGUAGE_INSTRUCTION.format(language=language_name(language))
                if language
                else _DETECT_LANGUAGE_INSTRUCTION
            ),
            # The specific conventions when the caller knows the language; the
            # catch-all when only the model will know it.
            swiss_conventions(language) if language else SWISS_CONVENTIONS_ANY,
        )
        if part
    )
    try:
        with client.messages.stream(
            model=model,
            # 8-12 questions with angles plus gaps and positioning; short caps
            # truncate mid-list (same lesson as tailor.py's max_tokens comment).
            max_tokens=8192,
            system=_system_blocks(profile_block, jd_block),
            messages=[
                {
                    "role": "user",
                    "content": _PREP_PROMPT.format(language_instruction=instruction),
                }
            ],
            output_format=PrepSheet,
        ) as stream:
            drain_stream(stream, on_delta)
            response = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - per-posting best effort
        logger.warning("Interview prep generation failed: %s", exc)
        return None
    if usage_acc is not None:
        usage.accumulate(usage_acc, response)
    if response.stop_reason == "max_tokens":
        logger.warning("Interview prep hit max_tokens; output may be truncated")
    return response.parsed_output


# Section headings per language, so the scaffolding around the sheet is in the
# same language as the sheet. Only the languages a Switzerland-focused search
# actually turns up are translated; anything else falls back to English
# headings over foreign-language content, which beats a guessed translation.
_HEADINGS: dict[str, dict[str, str]] = {
    "en": {
        "header_with_company": "Interview prep: {title} at {company}",
        "header": "Interview prep: {title}",
        "posting": "Posting",
        "positioning": 'Positioning ("tell me about yourself")',
        "questions": "Likely questions",
        "lead_with": "Lead with",
        "nothing_fits": "(nothing prepared fits)",
        "angle": "Angle",
        "gaps": "Gaps nothing prepared covers",
        "none_flagged": "none flagged",
        "write_ups": "Write-ups to re-read first",
        "ask": "Questions to ask them",
    },
    "de": {
        "header_with_company": "Interview-Vorbereitung: {title} bei {company}",
        "header": "Interview-Vorbereitung: {title}",
        "posting": "Inserat",
        "positioning": "Positionierung («Erzählen Sie von sich»)",
        "questions": "Wahrscheinliche Fragen",
        "lead_with": "Damit einsteigen",
        "nothing_fits": "(nichts Vorbereitetes passt)",
        "angle": "Ansatz",
        "gaps": "Lücken, die nichts Vorbereitetes abdeckt",
        "none_flagged": "keine markiert",
        "write_ups": "Write-ups, die vorher nochmals zu lesen sind",
        "ask": "Fragen an das Gegenüber",
    },
    "fr": {
        "header_with_company": "Préparation d'entretien : {title} chez {company}",
        "header": "Préparation d'entretien : {title}",
        "posting": "Annonce",
        "positioning": "Positionnement (« parlez-moi de vous »)",
        "questions": "Questions probables",
        "lead_with": "Commencer par",
        "nothing_fits": "(rien de préparé ne convient)",
        "angle": "Angle",
        "gaps": "Lacunes que rien de préparé ne couvre",
        "none_flagged": "aucune signalée",
        "write_ups": "Publications à relire en priorité",
        "ask": "Questions à leur poser",
    },
    "it": {
        "header_with_company": "Preparazione al colloquio: {title} presso {company}",
        "header": "Preparazione al colloquio: {title}",
        "posting": "Annuncio",
        "positioning": 'Posizionamento ("parlami di te")',
        "questions": "Domande probabili",
        "lead_with": "Iniziare con",
        "nothing_fits": "(niente di preparato calza)",
        "angle": "Angolazione",
        "gaps": "Lacune che nulla di preparato copre",
        "none_flagged": "nessuna segnalata",
        "write_ups": "Pubblicazioni da rileggere per prime",
        "ask": "Domande da porre loro",
    },
}


def render_prep_md(sheet: PrepSheet, title: str, company: str = "", url: str = "") -> str:
    h = _HEADINGS.get((sheet.language or "en").lower(), _HEADINGS["en"])
    header = (
        h["header_with_company"].format(title=title, company=company)
        if company
        else h["header"].format(title=title)
    )
    lines = [f"# {header}", ""]
    if url:
        lines += [f"- {h['posting']}: {url}", ""]
    lines += [
        f"## {h['positioning']}",
        "",
        sheet.positioning.strip(),
        "",
        f"## {h['questions']}",
        "",
    ]
    for q in sheet.likely_questions:
        lines.append(f"### {q.question}")
        if q.recommended_material:
            lines.append(f"- {h['lead_with']}: **{q.recommended_material}**")
        else:
            lines.append(f"- {h['lead_with']}: {h['nothing_fits']}")
        lines.append(f"- {h['angle']}: {q.angle}")
        lines.append("")
    lines += [f"## {h['gaps']}", ""]
    lines += [f"- {g}" for g in sheet.coverage_gaps] or [f"- {h['none_flagged']}"]
    if sheet.write_ups_to_revisit:
        lines += ["", f"## {h['write_ups']}", ""]
        lines += [f"- {w}" for w in sheet.write_ups_to_revisit]
    lines += ["", f"## {h['ask']}", ""]
    lines += [f"- {q}" for q in sheet.questions_to_ask]
    lines.append("")
    # Swiss orthography over the whole sheet, headings included: the model
    # writes the prose, this guarantees the one rule it keeps breaking.
    return swiss_spelling("\n".join(lines), sheet.language)

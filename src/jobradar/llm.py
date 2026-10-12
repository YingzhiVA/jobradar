"""Which Claude model each stage uses.

Every stage has a default and an environment override (documented in
.env.example). The override is read when the stage runs, not when its module
is imported: each entry point loads .env inside its ``run()``/``main()``, well
after the imports, so an import-time ``os.environ.get`` would silently ignore
a value set in .env and only honour one exported in the shell.
"""

from __future__ import annotations

import os

MODEL_DEFAULTS: dict[str, str] = {
    # The daily search
    # Haiku 5.5 since 1.11.0: on the same prompt it flipped fewer verdicts
    # between draws and cost about a quarter per call, thinking included
    # (dev/scoring-eval/NOTES.md, "Model A/B").
    "JOBRADAR_SCORING_MODEL": "claude-haiku-5-5",
    "JOBRADAR_WRITEUP_MODEL": "claude-sonnet-4-6",
    # Haiku 5.5 since 1.12.0, for web search and discovery alike: cheaper per
    # run and more productive (dev/websearch-eval/NOTES.md).
    "JOBRADAR_WEB_SEARCH_MODEL": "claude-haiku-5-5",
    # Monthly company discovery
    "JOBRADAR_DISCOVERY_MODEL": "claude-haiku-5-5",
    # The apply pipeline
    # JD extraction. Haiku 5.5 since 1.13.0: a fifth of the cost, faster, and a
    # more complete copy of the posting (dev/apply-eval/NOTES.md).
    "JOBRADAR_APPLY_HELPER_MODEL": "claude-haiku-5-5",
    "JOBRADAR_APPLY_WRITER_MODEL": "claude-sonnet-4-6",
    "JOBRADAR_APPLY_PREP_MODEL": "claude-sonnet-4-6",
}

# web_search_20260209 adds dynamic filtering (filters results before they hit
# the context). Models not listed get the basic web_search_20250305.
# claude-haiku-5-5 accepts the newer version, but is left out on purpose: in the
# 2026-10-12 comparison the model chose to filter in 1 of 3 runs, with no
# difference in what it found and slightly more time and cost.
DYNAMIC_FILTER_MODELS = {
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-fable-5",
}


def model_for(var: str) -> str:
    """The model id for a stage: its environment override if set (and not
    blank), else the default. ``var`` is one of the MODEL_DEFAULTS keys."""
    return os.environ.get(var) or MODEL_DEFAULTS[var]


def web_search_tool_type(model: str) -> str:
    """The web_search tool version this model supports."""
    return "web_search_20260209" if model in DYNAMIC_FILTER_MODELS else "web_search_20250305"

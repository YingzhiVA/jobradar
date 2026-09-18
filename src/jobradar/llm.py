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
    "JOBRADAR_SCORING_MODEL": "claude-haiku-4-5",
    "JOBRADAR_WRITEUP_MODEL": "claude-sonnet-4-6",
    "JOBRADAR_WEB_SEARCH_MODEL": "claude-haiku-4-5",
    # Monthly company discovery
    "JOBRADAR_DISCOVERY_MODEL": "claude-haiku-4-5",
    # The apply pipeline
    "JOBRADAR_APPLY_HELPER_MODEL": "claude-haiku-4-5",
    "JOBRADAR_APPLY_WRITER_MODEL": "claude-sonnet-4-6",
    "JOBRADAR_APPLY_PREP_MODEL": "claude-sonnet-4-6",
}

# web_search_20260209 adds dynamic filtering (filters results before they hit
# the context) but is only supported on these models; everything else (e.g.
# Haiku) must use the basic web_search_20250305.
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

"""Personal desk exports, loaded only when requested.

Deadline and cancellation helpers must remain independently importable by legacy
LLM/provider paths without constructing report or persistence services.
"""

from importlib import import_module

_EXPORTS = {
    "RankedEvent": "contracts",
    "article_matches_profile": "contracts",
    "normalize_phrase_tokens": "contracts",
    "rank_events": "contracts",
    "PersonalRepository": "repository",
    "create_daily_run": "runs",
    "retry_run": "runs",
    "OwnerBindingConflict": "workspace",
    "OwnerSelectionRequired": "workspace",
    "ensure_workspace": "workspace",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value

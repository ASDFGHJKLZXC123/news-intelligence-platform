"""Vocabulary and regime rules for historical-episode retrieval (historical-episode spec).

Pure: no database, no network, no exceptions. Callers decide what an unknown token *means*
(:mod:`services.analogies.contracts` turns it into a typed error, the event entry point turns it
into an abstention); this module only answers what the vocabulary says.

Two rules live here, and they are the two the spec puts *before* nearest-neighbour ordering:

* **Type compatibility.** An event is only ever matched against its own episode family. The map
  below is deliberately conservative -- no cross-family widening, no "close enough" pairs -- so a
  token the vocabulary does not know resolves to nothing at all. Searching all of history for an
  unrecognized event type is the one outcome that must never happen: it would return the nearest
  vectors in a corpus of unrelated crises and present them as analogies.
* **Regime gating.** An episode from another regime is *not* excluded and is *not* silently
  treated as comparable: it stays a candidate and carries the caveats item 3 must surface.
"""

from __future__ import annotations

from collections.abc import Iterable

from db.models.core import EPISODE_TYPES

#: Event-type tokens that name an episode family under a different word. Every entry is a
#: synonym, never a widening: `supply_chain` *is* the supply-shock family, whereas (say) a
#: company_distress event is not admitted to `industry_shock` merely because one can spill into
#: the other. Widening is the reranker's job (item 3), on candidates it can see and caveat.
EPISODE_TYPE_ALIASES: dict[str, str] = {
    "bank_run": "banking_stress",
    "corporate_distress": "company_distress",
    "sovereign_default": "sovereign_debt",
    "supply_chain": "supply_shock",
}

_SEPARATORS = ("-", "/", ".")


def normalize_token(value: str) -> str:
    """Fold a free-text type/tag token to the snake_case vocabulary form."""
    folded = value.strip().lower()
    for separator in _SEPARATORS:
        folded = folded.replace(separator, " ")
    return "_".join(folded.split())


def normalize_tags(values: Iterable[str] | None) -> frozenset[str]:
    """Normalize a tag collection, dropping blanks. ``None`` and empty mean the same thing."""
    if not values:
        return frozenset()
    return frozenset(token for token in (normalize_token(value) for value in values) if token)


def canonical_episode_type(value: str | None) -> str | None:
    """The episode family a token names, or ``None`` when the vocabulary does not know it."""
    if not value:
        return None
    token = normalize_token(value)
    token = EPISODE_TYPE_ALIASES.get(token, token)
    return token if token in EPISODE_TYPES else None


def episode_types_for_event_type(event_type: str | None) -> frozenset[str]:
    """The families a current event may be matched against. Empty means: do not search."""
    family = canonical_episode_type(event_type)
    return frozenset() if family is None else frozenset({family})


def partition_episode_types(values: Iterable[str]) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split requested types into the canonical families and the tokens nothing recognized."""
    known: set[str] = set()
    unknown: list[str] = []
    for value in values:
        family = canonical_episode_type(value)
        if family is None:
            unknown.append(value)
        else:
            known.add(family)
    return frozenset(known), tuple(sorted(unknown))


def regime_caveats(
    current_tags: Iterable[str] | None, episode_tags: Iterable[str] | None
) -> tuple[bool, tuple[str, ...]]:
    """Whether an episode needs explicit regime caveats, and deterministically why.

    The spec's gate: an episode sharing at least one of the current regime's tags is normal; one
    that shares none of them stays a candidate but must be caveated. An episode carrying no tags
    at all shares none either -- unverified is not the same as comparable, so it is caveated too.

    When the current event has no regime tags there is nothing to disagree with, and no mismatch
    is manufactured: every candidate comes back uncaveated.
    """
    current = normalize_tags(current_tags)
    episode = normalize_tags(episode_tags)
    if not current or current & episode:
        return False, ()
    if not episode:
        return True, ("episode carries no regime tags; regime comparability is unverified",)
    return True, (
        "episode shares none of the current regime tags "
        f"(current: {', '.join(sorted(current))}; episode: {', '.join(sorted(episode))})",
    )


__all__ = [
    "EPISODE_TYPE_ALIASES",
    "canonical_episode_type",
    "episode_types_for_event_type",
    "normalize_tags",
    "normalize_token",
    "partition_episode_types",
    "regime_caveats",
]

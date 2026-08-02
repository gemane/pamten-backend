"""
Scraper registry — the dispatch layer for the multi-scraper platform (Phase 2).

Each scraper registers a `ScraperSpec` (its name, an `enabled()` predicate, and a
`run(query, depth) -> dict`). `run_scrape_all` iterates the registry instead of a
hardcoded per-source if/elif chain, so adding a scraper is "register a spec", not
"edit the orchestrator". A new scraper in its own module just calls `register(...)`
on import — nothing in the dispatch needs touching.

Registration is keyed by name, so re-registering replaces (re-imports during tests
never duplicate entries), and iteration order is registration order.
"""
from dataclasses import dataclass
from typing import Callable, Literal

_registry: dict[str, "ScraperSpec"] = {}


@dataclass(frozen=True)
class ScraperSpec:
    name: str                            # result key + source identifier, e.g. "wikidata"
    run: Callable[[str, int], dict]      # (query, depth) -> the scraper's result dict
    enabled: Callable[[], bool]          # master flag + per-source toggle check
    # Source KIND: "instant" = query-driven, per-company, safe to run on demand
    # (Wikidata, SEC EDGAR, OpenCorporates); "bulk" = whole-dataset scheduled import
    # (GLEIF). The on-demand search runs ONLY enabled "instant" sources — never "bulk".
    kind: Literal["instant", "bulk"] = "instant"
    # Does this source traverse ownership depth? Only depth-aware sources (Wikidata)
    # are re-run on the idle depth-2 "deepen" pass; depth-blind ones (SEC/OpenCorporates)
    # ignore depth, so re-running them would be wasted work.
    depth_aware: bool = False


def register(spec: ScraperSpec) -> None:
    """Add (or replace, by name) a scraper in the registry."""
    _registry[spec.name] = spec


def registered() -> list[ScraperSpec]:
    """The registered scrapers, in registration order."""
    return list(_registry.values())


def get(name: str) -> "ScraperSpec | None":
    return _registry.get(name)

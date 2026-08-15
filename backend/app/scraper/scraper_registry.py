"""
Scraper registry — the dispatch layer for the multi-scraper platform (Phase 2).

Each scraper registers a `ScraperSpec` (its name, an `enabled()` predicate, and a
`run(query, depth, country) -> dict`). `run_scrape_all` iterates the registry instead of a
hardcoded per-source if/elif chain, so adding a scraper is "register a spec", not
"edit the orchestrator". A new scraper in its own module just calls `register(...)`
on import — nothing in the dispatch needs touching.

Registration is keyed by name, so re-registering replaces (re-imports during tests
never duplicate entries), and iteration order is registration order.
"""
import inspect
from dataclasses import dataclass
from typing import Callable, Literal

_registry: dict[str, "ScraperSpec"] = {}


@dataclass(frozen=True)
class ScraperSpec:
    name: str                            # result key + source identifier, e.g. "wikidata"
    # (query, depth, country) -> the scraper's result dict.
    #
    # `country` is an ISO-2 the user picked in the search box, or None. A source
    # that can tell where its match is MUST reject a match in another country —
    # `country_match.matches_requested` is the shared rule, and
    # `country_match.country_mismatch` the result to return. Asked for Germany,
    # every source left to itself answers "Alphabet" with Alphabet Inc of
    # Mountain View; the country is the only thing that stops it being written.
    run: Callable[[str, int, str | None], dict]
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
    """Add (or replace, by name) a scraper in the registry.

    The `run` callable is checked here for one reason: the dispatchers catch
    every exception per source, so one scraper failing cannot sink the others.
    A `run` that cannot take `(query, depth, country)` therefore raises a
    TypeError that is caught, logged and moved past — the scrape "succeeds"
    having run nothing at all. Registration is the last place that mistake is
    still loud.
    """
    try:
        inspect.signature(spec.run).bind("q", 0, None)
    except TypeError as exc:
        raise TypeError(
            f"scraper {spec.name!r}: run must accept (query, depth, country) — {exc}"
        ) from None
    _registry[spec.name] = spec


def registered() -> list[ScraperSpec]:
    """The registered scrapers, in registration order."""
    return list(_registry.values())


def get(name: str) -> "ScraperSpec | None":
    return _registry.get(name)

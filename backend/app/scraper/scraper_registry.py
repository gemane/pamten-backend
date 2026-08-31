"""
Scraper registry — the dispatch layer for the multi-scraper platform (Phase 2,
enriched in the modularisation follow-up).

Each scraper registers a `ScraperSpec`; `run_scrape_all` iterates the registry
instead of a hardcoded per-source if/elif chain, so adding a scraper is
"register a spec", not "edit the orchestrator".

The enrichment: a spec is now a COMPLETE declaration. Its display metadata
(label, home URL, credibility, quality band) comes from its `KNOWN_SOURCES`
catalogue entry via `spec.meta`, its `enabled()` check is derived — the
`SCRAPER_<NAME>_ENABLED` settings flag AND the per-source DB toggle — unless
explicitly overridden, and `register()` cross-validates the pieces that used to
be able to drift silently: the catalogue entry must exist, its `kind` must match
the spec's, and the settings flag must actually be defined on `Settings`. Four
files that had to agree by convention became one declaration checked loudly at
import time.

Registration is keyed by name, so re-registering replaces (re-imports during
tests never duplicate entries), and iteration order is registration order.
"""
import dataclasses
import inspect
from dataclasses import dataclass, field
from typing import Callable, Literal

from app.config import settings
from app.scraper.sources import KNOWN_SOURCES, get_source_enabled

_registry: dict[str, "ScraperSpec"] = {}


@dataclass(frozen=True)
class ScraperSpec:
    name: str                            # result key + KNOWN_SOURCES catalogue key
    # (query, depth, country) -> the scraper's result dict.
    #
    # `country` is an ISO-2 the user picked in the search box, or None. A source
    # that can tell where its match is MUST reject a match in another country —
    # `country_match.matches_requested` is the shared rule, and
    # `country_match.country_mismatch` the result to return. Asked for Germany,
    # every source left to itself answers "Alphabet" with Alphabet Inc of
    # Mountain View; the country is the only thing that stops it being written.
    run: Callable[[str, int, str | None], dict]
    # None (the default) derives the standard check — the settings flag AND the
    # per-source DB toggle. Only pass a callable to do something unusual.
    enabled: Callable[[], bool] | None = field(default=None)
    # Source KIND: "instant" = query-driven, per-company, safe to run on demand
    # (Wikidata, SEC EDGAR, OpenCorporates); "bulk" = whole-dataset scheduled import
    # (GLEIF). The on-demand search runs ONLY enabled "instant" sources — never "bulk".
    kind: Literal["instant", "bulk"] = "instant"
    # Does this source traverse ownership depth? Only depth-aware sources (Wikidata)
    # are re-run on the idle depth-2 "deepen" pass; depth-blind ones (SEC/OpenCorporates)
    # ignore depth, so re-running them would be wasted work.
    depth_aware: bool = False
    # The Settings attribute gating this source. Defaults to
    # SCRAPER_<NAME>_ENABLED; declared explicitly where history disagrees with
    # the convention (open_corporates -> SCRAPER_OPENCORPORATES_ENABLED).
    settings_flag: str | None = None

    @property
    def flag_name(self) -> str:
        return self.settings_flag or f"SCRAPER_{self.name.upper()}_ENABLED"

    @property
    def meta(self) -> dict:
        """The catalogue entry: label / url / credibility / quality / description.

        One definition per source — the public catalogue, the Source node the
        writers create, and the credibility stamped on scraped data all read
        this, so they cannot disagree.
        """
        return KNOWN_SOURCES[self.name]

    @property
    def label(self) -> str:
        return self.meta["label"]

    @property
    def credibility(self) -> int:
        return self.meta["credibility"]

    @property
    def url(self) -> str:
        return self.meta["url"]


def _default_enabled(spec: ScraperSpec) -> Callable[[], bool]:
    """The standard two-part gate: env flag AND per-source DB toggle.

    The flag short-circuits, so with it off the DB is never touched — and the
    toggle is read through THIS module's `get_source_enabled` binding, which is
    the one canonical patch target for tests
    (`app.scraper.scraper_registry.get_source_enabled`).
    """
    def check() -> bool:
        return bool(getattr(settings, spec.flag_name)) and get_source_enabled(spec.name)
    return check


def register(spec: ScraperSpec) -> None:
    """Add (or replace, by name) a scraper in the registry — validating loudly.

    The dispatchers catch every exception per source, so one scraper failing
    cannot sink the others. That safety net makes registration the LAST place a
    wiring mistake is still loud, which is why everything checkable is checked
    here rather than discovered as a scrape that "succeeds" having run nothing:

    - `run` must accept (query, depth, country) — a signature mismatch used to
      silently empty four test files.
    - The `KNOWN_SOURCES` catalogue entry must exist: without it the writers
      have no label/credibility to stamp and the sources API would 500.
    - The spec's `kind` must MATCH the catalogue's — it was declared in both
      places and could drift.
    - The settings flag must exist on `Settings`, or the default `enabled()`
      would raise at scrape time — for every scrape, forever, as "source off".
    """
    try:
        inspect.signature(spec.run).bind("q", 0, None)
    except TypeError as exc:
        raise TypeError(
            f"scraper {spec.name!r}: run must accept (query, depth, country) — {exc}"
        ) from None
    if spec.name not in KNOWN_SOURCES:
        raise ValueError(
            f"scraper {spec.name!r}: no KNOWN_SOURCES catalogue entry — add one in "
            f"app/scraper/sources.py (label, url, credibility, quality) first")
    if KNOWN_SOURCES[spec.name]["kind"] != spec.kind:
        raise ValueError(
            f"scraper {spec.name!r}: kind {spec.kind!r} disagrees with the catalogue's "
            f"{KNOWN_SOURCES[spec.name]['kind']!r} — one declaration is wrong")
    if spec.enabled is None:
        # The flag only matters when the DEFAULT gate is derived from it — a
        # custom enabled() owns its own logic and may not use a flag at all.
        if not hasattr(settings, spec.flag_name):
            raise ValueError(
                f"scraper {spec.name!r}: Settings has no {spec.flag_name} — declare "
                f"the flag in app/config.py or pass settings_flag= explicitly")
        spec = dataclasses.replace(spec, enabled=_default_enabled(spec))
    _registry[spec.name] = spec


def registered() -> list[ScraperSpec]:
    """The registered scrapers, in registration order."""
    return list(_registry.values())


def get(name: str) -> "ScraperSpec | None":
    return _registry.get(name)

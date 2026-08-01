"""
GLEIF reference code-list lookups — resolve the codes stored in LEI-CDF records to
human-readable names, the way gleif.org displays them:

- **ELF** (ISO 20275 Entity Legal Form): `EntityLegalFormCode` (e.g. `H0PO`) → a legal
  form name (e.g. "Private Limited Company").
- **RA** (GLEIF Registration Authorities): `RegistrationAuthorityID` (e.g. `RA000585`)
  → the register's name (e.g. "Companies Register").

The lists are small, static reference data published by GLEIF separately from the
golden copy, bundled here as compact `data/gleif_{elf,ra}.json` (code → name). Refresh
them from https://www.gleif.org/en/about-lei/code-lists when GLEIF publishes a new
version; the build script lives in this module's git history / docs.
"""
import functools
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DATA = Path(__file__).parent / "data"


@functools.lru_cache(maxsize=1)
def _load(name: str) -> dict[str, str]:
    try:
        return json.loads((_DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # missing/corrupt bundle — degrade to codes
        log.warning("GLEIF reference list %s unavailable: %s", name, exc)
        return {}


def legal_form_name(code: str | None) -> str | None:
    """ELF code → legal form name, or None if the code is unknown/empty."""
    return _load("gleif_elf.json").get(code) if code else None


def registration_authority_name(code: str | None) -> str | None:
    """Registration Authority code → register name, or None if unknown/empty."""
    return _load("gleif_ra.json").get(code) if code else None

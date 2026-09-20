"""
Regenerate app/scraper/data/gleif_ra.json from GLEIF's Registration Authorities
list — the reference data behind `registration_authority_name()`,
`sole_register_for_country()` and `make_register_id()`.

GLEIF publishes the list at
https://www.gleif.org/en/lei-data/code-lists/gleif-registration-authorities-list
as a CSV (new version every year or two). Run this with the CSV's URL or a
downloaded path and commit the regenerated bundle:

    python scripts/build_gleif_ra_bundle.py \
        https://www.gleif.org/lei-data/code-lists/gleif-registration-authorities-list/2024-11-20_ra-list-v1.8.1.csv

Output shape (one entry per RA code):

    {"RA000585": {"name": "Companies Register", "countries": ["GB"]}}
    {"RA000602": {"name": "…", "countries": ["US"], "jurisdictions": ["Delaware"]}}

- ``name`` is the International name of Register, falling back to the
  organisation's international name — the same rule the old flat bundle used,
  so `registration_authority_name()` keeps returning what gleif.org displays.
- ``countries`` is every ISO-2 Country Code the register serves. A handful of
  codes span several countries (OHADA's RCCM covers 8; Sirene covers FR plus
  its territories) — one entry per code, countries merged and sorted.
- Rows with neither register nor organisation name are skipped. That drops the
  three placeholder codes (RA777777 public legal documents, RA888888 temporary,
  RA999999 no registration authority available), which must never identify a
  company; `make_register_id()` also excludes them explicitly in case a stale
  bundle leaks them back.
"""
import csv
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent / "app" / "scraper" / "data" / "gleif_ra.json"
DEFAULT_CSV = ("https://www.gleif.org/lei-data/code-lists/"
               "gleif-registration-authorities-list/2024-11-20_ra-list-v1.8.1.csv")


def read_rows(source: str) -> list[dict]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source) as resp:  # noqa: S310 — gleif.org, run by hand
            text = resp.read().decode("utf-8-sig")    # the file ships with a BOM
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def build(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        code = (row.get("Registration Authority Code") or "").strip()
        name = ((row.get("International name of Register") or "").strip()
                or (row.get("International name of organisation responsible for the Register") or "").strip())
        country = (row.get("Country Code") or "").strip().upper()
        if not code or not name:
            continue
        entry = out.setdefault(code, {"name": name, "countries": [], "jurisdictions": [],
                                      "names": [], "site": None})
        if country and country not in entry["countries"]:
            entry["countries"].append(country)
        # Every name the register or its organisation goes by — international
        # and local, several languages ("In French: …; in German: …") split
        # apart — plus the site's domain. This is what lets a filer's own
        # words ("Kamer van Koophandel", "Firmenbuch", "CSSF") name the
        # register; see gleif_reference.register_for_name().
        for col in ("International name of Register", "Local name of Register",
                    "International name of organisation responsible for the Register",
                    "Local name of organisation responsible for the Register"):
            for alias in _split_names(row.get(col) or ""):
                if alias not in entry["names"]:
                    entry["names"].append(alias)
        if not entry["site"]:
            entry["site"] = _domain(row.get("Website") or "")
        # The sub-national jurisdiction ("Delaware", "Ontario", …) — kept only
        # when it names a region rather than repeating the country, because it
        # is what lets a source that states a US STATE (not just "US")
        # contribute a register_id. See gleif_reference.register_for_place().
        jur = (row.get("Jurisdiction (country or region)") or "").strip()
        if jur and jur.casefold() != (row.get("Country") or "").strip().casefold() \
                and jur not in entry["jurisdictions"]:
            entry["jurisdictions"].append(jur)
    for entry in out.values():
        entry["countries"].sort()
        entry["jurisdictions"].sort()
        if not entry["jurisdictions"]:
            del entry["jurisdictions"]
        if not entry["site"]:
            del entry["site"]
    return dict(sorted(out.items()))


def _split_names(raw: str) -> list[str]:
    """"In French: Registre IDE; in German: UID-Register" → both names, no labels."""
    out: list[str] = []
    for part in re.split(r"[;\n]", raw):
        part = re.sub(r"^\s*in\s+[A-Za-z]+\s*:\s*", "", part.strip(), flags=re.IGNORECASE).strip()
        if part and part not in out:
            out.append(part)
    return out


def _domain(url: str) -> str | None:
    """"https://www.kvk.nl/english" → "kvk.nl"."""
    host = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0]
    host = re.sub(r"^www\.", "", host)
    return host or None


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    bundle = build(read_rows(source))
    BUNDLE.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":"),
                                 indent=None) + "\n", encoding="utf-8")
    multi = sum(1 for e in bundle.values() if len(e["countries"]) > 1)
    print(f"{len(bundle)} registration authorities -> {BUNDLE}")
    print(f"{multi} codes span more than one country")


if __name__ == "__main__":
    main()

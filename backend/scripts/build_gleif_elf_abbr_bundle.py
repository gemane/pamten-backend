"""
Regenerate app/scraper/data/gleif_elf_abbr.json from GLEIF's ISO 20275 Entity
Legal Forms code list — the reference data behind
`gleif_reference.legal_form_abbreviations()`.

GLEIF publishes the list (CC0) at
https://www.gleif.org/en/lei-data/code-lists/iso-20275-entity-legal-forms-code-list
as a CSV (a new version every year or two). Run this with the CSV's URL or a
downloaded path and commit the regenerated bundle:

    python scripts/build_gleif_elf_abbr_bundle.py \\
        https://www.gleif.org/lei-data/code-lists/iso-20275-entity-legal-forms-code-list/2026-02-19-elf-code-list-v1.6.csv

Output shape (one entry per ELF code that has abbreviations):

    {"R85P": ["AG", "NV", "SA"]}

Every language's abbreviations of a code are merged (Belgium's R85P is "NV",
"SA" and "AG"), local and transliterated, ";"-separated in the CSV; inactive
codes are kept, because an old record can still carry one. Why: GLEIF often
gives a legal name WITHOUT its form, and keeps the form as this code —
"ANHEUSER-BUSCH INBEV", R85P — where EDGAR writes "Anheuser-Busch InBev SA/NV"
(`legal_identity`, findings item 19).
"""
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent / "app" / "scraper" / "data" / "gleif_elf_abbr.json"
DEFAULT_CSV = ("https://www.gleif.org/lei-data/code-lists/"
               "iso-20275-entity-legal-forms-code-list/2026-02-19-elf-code-list-v1.6.csv")


def read_rows(source: str) -> list[dict]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source) as resp:  # noqa: S310 — gleif.org, run by hand
            text = resp.read().decode("utf-8-sig")    # the file ships with a BOM
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def build(rows: list[dict]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for row in rows:
        code = (row.get("ELF Code") or "").strip()
        if not code:
            continue
        for col in ("Abbreviations Local language", "Abbreviations transliterated"):
            for abbr in (row.get(col) or "").split(";"):
                if abbr.strip():
                    out.setdefault(code, set()).add(abbr.strip())
    return {code: sorted(abbrs) for code, abbrs in sorted(out.items())}


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    bundle = build(read_rows(source))
    BUNDLE.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":"),
                                 indent=None) + "\n", encoding="utf-8")
    print(f"{len(bundle)} legal forms with abbreviations -> {BUNDLE}")


if __name__ == "__main__":
    main()

"""
Regenerate app/scraper/data/industry_nace.json — the reference data behind
`app.industry`: the NACE Rev. 2 tree every company's sector is shown in, and
the two source code lists (SEC's US SIC, Companies House's UK SIC 2007) with
their mapping onto it.

Owlgraph displays ONE tree, NACE Rev. 2 (section > division > group > class),
and keeps each source's own code beside it. The sources:

  NACE Rev. 2     Eurostat's SDMX codelist NACE_R2 (EN + DE labels)
                  https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/codelist/ESTAT/NACE_R2?format=JSON&lang=EN
                  Eurostat data: free re-use, CC BY 4.0 (source acknowledged).
  US SIC (SEC)    the SIC codes EDGAR assigns to filers, with the Division of
                  Corporation Finance office — public domain
                  https://www.sec.gov/corpfin/division-of-corporation-finance-standard-industrial-classification-sic-code-list
  UK SIC 2007     Companies House's condensed list — Open Government Licence v3
                  https://resources.companieshouse.gov.uk/sic/
  US SIC → NACE   US Census concordances, public domain: 1987 SIC → 2002 NAICS
                  → 2007 → 2012 → 2017 NAICS → ISIC Rev. 4
                  https://www.census.gov/naics/concordances/…
                  then the UN's ISIC Rev. 4 → NACE Rev. 2 table
                  https://unstats.un.org/unsd/classifications/Econ/tables/ISIC/ISIC4_NACE2/ISIC4_NACE2.txt

Run with a directory holding the downloaded files (names below) and commit
the regenerated bundle:

    python scripts/build_industry_bundle.py /path/to/downloads

Files expected in that directory: nace_estat.json, nace_DE.json, sec_sic.html,
ch_sic.html, sic_naics.xls, naics02_07.xls, naics07_12.xls, naics12_17.xlsx,
naics17_isic4.xlsx, ISIC4_NACE2.txt. Needs `xlrd` and `openpyxl` (not app
dependencies — install them in a throwaway venv for this script).

Mapping rules, which the tests in tests/test_industry.py pin:

- A UK SIC 2007 subclass is a NACE class plus a fifth digit: 23320 → 23.32.
  When the four digits are no NACE class (a UK regrouping), the group or the
  division. Three UK-only codes mean nothing in NACE and map to NOTHING:
  74990 "Non-trading company", 98000 "Residents property management", 99999
  "Dormant company" — the register's label is still shown.
- A US SIC code reaches NACE through a chain of one-to-many tables, so it
  ends at the DEEPEST NACE node all its paths share: the class when they
  agree, else the group, the division, the section — or nothing. SIC 7370
  (computer programming, data processing) fans out over software, hosting
  and portals and stops at section J. The code and the SEC's title are kept
  beside it, so the reader sees what the SEC said, however coarse the tree.
"""
import csv
import html
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent / "app" / "scraper" / "data" / "industry_nace.json"

#: UK SIC 2007 codes with no NACE meaning (Companies House additions).
UK_ONLY = {"74990", "98000", "99999"}

#: SEC codes the crosswalk cannot settle, decided by hand — each with its reason.
US_SIC_OVERRIDES: dict[str, str | None] = {
    # The Census splits "Prepackaged software" evenly between software
    # publishing (J) and the mass reproduction of media (C); a tie. The
    # filers under it are publishers — Microsoft, Oracle, Adobe.
    "7372": "58.29",
    # A blank-check company has no activity to classify until it merges.
    "6770": None,
    # Data processing: NAICS 518210 corresponds to ISIC 6311 (data
    # processing, hosting) and, in a sliver, 7420 (photographic activities);
    # the Census table weighs the two the same.
    "7374": "63.11",
    # Water supply: NAICS 221310 is ISIC 3600 (water collection, treatment
    # and supply) with a sliver of 4930 (pipeline transport).
    "4941": "36.00",
}


# ── NACE ─────────────────────────────────────────────────────────────────────

def nace_code(key: str) -> str | None:
    """Eurostat's key → the printed code: C → C, C23 → 23, C233 → 23.3, C2332 → 23.32."""
    m = re.fullmatch(r"([A-U])(\d{0,4})", key)
    if not m:
        return None
    letter, digits = m.groups()
    if not digits:
        return letter
    if len(digits) == 2:
        return digits
    if len(digits) == 3:
        return f"{digits[:2]}.{digits[2]}"
    if len(digits) == 4:
        return f"{digits[:2]}.{digits[2:]}"
    return None


def read_nace(en_path: Path, de_path: Path) -> tuple[dict, dict]:
    en = json.loads(en_path.read_text())
    de = json.loads(de_path.read_text())["category"]["label"]
    labels = en["category"]["label"]
    nodes: dict[str, dict] = {}
    for key, label in labels.items():
        code = nace_code(key)
        if not code:
            continue
        level = 1 if len(key) == 1 else len(key) - 1
        parent = (None if level == 1 else key[0] if level == 2
                  else code[:2] if level == 3 else code[:4])
        nodes[code] = {"label": {"en": label, "de": de.get(key, label)},
                       "level": level, "parent": parent}
    ext = en.get("extension", {})
    updated = next((a.get("title") for a in ext.get("annotation", [])
                    if a.get("type") == "LAST_UPDATED"), None)
    meta = {"codelist": ext.get("id"), "version": ext.get("version"), "updated": updated}
    return nodes, meta


# ── Source lists ─────────────────────────────────────────────────────────────

def read_sec_sic(path: Path) -> dict[str, dict]:
    """(code, office, title) rows of the SEC's list → {"3250": {"label", "office"}}."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", path.read_text(), re.S)
    out: dict[str, dict] = {}
    for row in rows:
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 3 and cells[0].isdigit():
            out[cells[0].zfill(4)] = {"label": cells[2], "office": cells[1]}
    return out


def read_uk_sic(path: Path) -> dict[str, str]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", path.read_text(), re.S)
    out: dict[str, str] = {}
    for row in rows:
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 2 and re.fullmatch(r"\d{5}", cells[0]):
            out[cells[0]] = cells[1]
    return out


# ── Crosswalks ───────────────────────────────────────────────────────────────

def _code(v, width: int = 0) -> str | None:
    """A spreadsheet cell holding a code: 111.0 → '111', padded to `width`
    when the scheme has leading zeros the sheet dropped (SIC 0111, ISIC 0610)."""
    if v is None or v == "":
        return None
    if isinstance(v, float):
        v = int(v)
    s = str(v).strip()
    return s.zfill(width) if re.fullmatch(r"\d+", s) else None


def read_sic_naics(path: Path) -> dict[str, dict[str, int]]:
    """SIC → {NAICS 2002: weight}. The Census table splits an industry over
    several NAICS codes, each row saying which part in the title: "Aircraft
    (except research and development…)" → 336411, "Aircraft (research and
    development…)" → 541710. The "(except …)" row is the industry's main
    part and weighs 3; every other row 1."""
    import xlrd
    sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for i in range(1, sheet.nrows):
        sic, title, naics = (sheet.cell_value(i, 0), str(sheet.cell_value(i, 1)),
                             sheet.cell_value(i, 2))
        left, right = _code(sic, 4), _code(naics)
        if left and right:
            out[left][right] = 3 if "(except" in title.lower() else 1
    return out


def _xls_pairs(path: Path, left_header: str, right_header: str,
               widths: tuple[int, int] = (0, 0)) -> dict[str, set[str]]:
    import xlrd
    sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
    header_row, lcol, rcol = None, None, None
    for i in range(min(sheet.nrows, 10)):
        cells = [str(c.value).strip() for c in sheet.row(i)]
        if any(c.startswith(left_header) for c in cells):
            header_row = i
            lcol = next(j for j, c in enumerate(cells) if c.startswith(left_header))
            rcol = next(j for j, c in enumerate(cells) if c.startswith(right_header))
            break
    assert header_row is not None, (path, left_header)
    out: dict[str, set[str]] = defaultdict(set)
    for i in range(header_row + 1, sheet.nrows):
        left = _code(sheet.cell_value(i, lcol), widths[0])
        right = _code(sheet.cell_value(i, rcol), widths[1])
        if left and right:
            out[left].add(right)
    return out


def _xlsx_pairs(path: Path, left_header: str, right_header: str,
                widths: tuple[int, int] = (0, 0)) -> dict[str, set[str]]:
    import openpyxl
    ws = openpyxl.load_workbook(str(path), read_only=True).worksheets[0]
    lcol = rcol = None
    out: dict[str, set[str]] = defaultdict(set)
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if lcol is None:
            if any(re.sub(r"\s+", " ", c).startswith(left_header) for c in cells):
                lcol = next(j for j, c in enumerate(cells) if re.sub(r"\s+", " ", c).startswith(left_header))
                rcol = next(j for j, c in enumerate(cells) if re.sub(r"\s+", " ", c).startswith(right_header))
            continue
        left, right = _code(row[lcol], widths[0]), _code(row[rcol], widths[1])
        if left and right:
            out[left].add(right)
    assert lcol is not None, (path, left_header)
    return out


def read_naics_isic(path: Path) -> dict[str, dict[str, int]]:
    """2017 NAICS → {ISIC Rev. 4: 1}. Every ISIC row of a NAICS industry
    weighs the same: the sheet's "Part of" flags say an ISIC class is wholly
    inside the NAICS (2391 refractories inside 327120 clay building
    materials and refractories), not which part of the NAICS is bigger —
    read as a weight they made every SEC clay-products filer a refractory
    maker. Where equal shares tie, `US_SIC_OVERRIDES` decides."""
    import openpyxl
    ws = openpyxl.load_workbook(str(path), read_only=True).worksheets[0]
    out: dict[str, dict[str, int]] = defaultdict(dict)
    started = False
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if not started:
            started = any(re.sub(r"\s+", " ", c).startswith("2017 NAICS US") for c in cells)
            continue
        naics, isic = _code(row[1]), _code(row[4], 4)
        if naics and isic and naics != "0":
            out[naics][isic] = 1
    return out


def read_isic_nace(path: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            isic, nace = row["ISIC4code"].strip(), row["NACE2code"].strip()
            if re.fullmatch(r"\d{4}", isic) and re.fullmatch(r"\d{2}\.\d{2}", nace):
                out[isic].add(nace)
    return out


def ancestors(code: str) -> list[str]:
    """section, division, group, class of a NACE class code — needs the tree's
    letters, so the caller passes a lookup; here only the numeric chain."""
    return [code[:2], code[:4], code] if len(code) == 5 else [code]


def majority_node(weights: dict[str, float], section_of: dict[str, str]) -> str | None:
    """The deepest NACE node holding MORE THAN HALF of a code's weight.

    `weights` is NACE class → share of the SIC code's paths. Descend section →
    division → group → class while one node carries a strict majority of the
    whole; stop at the last that does. SIC 5812 "Eating places" reaches six
    NACE 56 classes and one of 90 (dinner theaters): section I and division
    56 hold 6/7, no group holds half, so the code is 56. A tie (7372
    "Prepackaged software": publishing versus reproduction) descends nowhere."""
    total = sum(weights.values())
    if not total:
        return None
    chosen = None
    for level in (lambda c: section_of.get(c[:2]), lambda c: c[:2], lambda c: c[:4], lambda c: c):
        tally: dict[str, float] = defaultdict(float)
        for cls, w in weights.items():
            node = level(cls)
            if node and (chosen is None or node.startswith(chosen) or
                         (len(chosen) == 1 and section_of.get(node[:2]) == chosen)):
                tally[node] += w
        node, w = max(tally.items(), key=lambda kv: kv[1], default=(None, 0))
        if node is None or w <= total / 2:
            break
        chosen = node
    return chosen


def expand_sec_code(code: str, sic_codes: set[str]) -> set[str]:
    """The 1987 SIC industries behind an SEC code. EDGAR assigns many filers
    a GROUP code — 3250 for structural clay products, 7370 for computer
    services — which the 1987 list has no industry for: it has 3251, 3253,
    3255, 3259. Such a code stands for every industry under its prefix (the
    digits before the trailing zeros); a code the list has is itself."""
    if code in sic_codes:
        return {code}
    prefix = code.rstrip("0")
    if not prefix:
        return set()
    return {c for c in sic_codes if c.startswith(prefix)}


def nace_weights(sic: str, sic_naics: dict[str, dict[str, int]], chain: list[dict],
                 isic_nace: dict[str, set[str]]) -> dict[str, float]:
    """NACE class → weight for one 1987 SIC industry: each SIC→NAICS row's
    weight follows the NAICS vintages (split equally where a code forks) and
    is split equally over the NACE classes its ISIC class corresponds to."""
    weights: dict[str, float] = defaultdict(float)
    for naics, w in sic_naics.get(sic, {}).items():
        current = {naics: float(w)}
        for table in chain:
            nxt: dict[str, float] = defaultdict(float)
            for c, cw in current.items():
                targets = table.get(c, ())
                if not isinstance(targets, dict):
                    targets = {n: 1 for n in targets}
                share = sum(targets.values())
                for n, w in targets.items():
                    nxt[n] += cw * w / share
            current = nxt
        for isic, iw in current.items():
            classes = isic_nace.get(isic, ())
            for cls in classes:
                weights[cls] += iw / len(classes)
    return weights


def map_us_sic(sec: dict[str, dict], sic_naics: dict[str, dict[str, int]], chain: list[dict],
               isic_nace: dict[str, set[str]], section_of: dict[str, str]) -> tuple[dict[str, dict], dict[str, int]]:
    """Walk each SEC SIC code through the chain and reduce to one NACE node."""
    out: dict[str, dict] = {}
    depth: dict[str, int] = defaultdict(int)
    for code, entry in sec.items():
        # Each 1987 industry under an SEC group code counts once, whatever
        # the size of its own fan-out: 3250 is 3251, 3253, 3255 and 3259 at
        # a quarter each, not the refractory industry's single 1:1 row
        # outweighing the three brick and tile ones.
        weights: dict[str, float] = defaultdict(float)
        for sic in expand_sec_code(code, set(sic_naics)):
            own = nace_weights(sic, sic_naics, chain, isic_nace)
            total = sum(own.values())
            for cls, w in own.items():
                weights[cls] += w / total
        nace = US_SIC_OVERRIDES.get(code, majority_node(weights, section_of))
        out[code] = {"label": entry["label"], "office": entry["office"], "nace": nace}
        depth[{None: "none", 1: "section", 2: "division", 4: "group", 5: "class"}[len(nace) if nace else None]] += 1
    return out, dict(depth)


def map_uk_sic(uk: dict[str, str], nace: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for code, label in uk.items():
        if code in UK_ONLY:
            out[code] = {"label": label, "nace": None}
            continue
        cls, grp, div = f"{code[:2]}.{code[2:4]}", f"{code[:2]}.{code[2]}", code[:2]
        out[code] = {"label": label,
                     "nace": cls if cls in nace else grp if grp in nace else div if div in nace else None}
    return out


def build(downloads: Path) -> dict:
    nace, meta = read_nace(downloads / "nace_estat.json", downloads / "nace_DE.json")
    section_of = {code: node["parent"] for code, node in nace.items() if node["level"] == 2}
    sic_naics = read_sic_naics(downloads / "sic_naics.xls")
    chain = [
        _xls_pairs(downloads / "naics02_07.xls", "2002 NAICS Code", "2007 NAICS Code"),
        _xls_pairs(downloads / "naics07_12.xls", "2007 NAICS Code", "2012 NAICS Code"),
        _xlsx_pairs(downloads / "naics12_17.xlsx", "2012 NAICS Code", "2017 NAICS Code"),
        read_naics_isic(downloads / "naics17_isic4.xlsx"),
    ]
    us, depth = map_us_sic(read_sec_sic(downloads / "sec_sic.html"), sic_naics, chain,
                           read_isic_nace(downloads / "ISIC4_NACE2.txt"), section_of)
    uk = map_uk_sic(read_uk_sic(downloads / "ch_sic.html"), nace)
    print(f"NACE nodes: {len(nace)}  US SIC: {len(us)} → {depth}  UK SIC: {len(uk)} "
          f"({sum(1 for v in uk.values() if v['nace'] is None)} unmapped)", file=sys.stderr)
    return {
        "built": date.today().isoformat(),
        "nace_source": meta,
        "nace": nace,
        "schemes": {
            "us-sic": {"name": "US SIC (SEC EDGAR)", "codes": us},
            "uk-sic": {"name": "UK SIC 2007", "codes": uk},
        },
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    bundle = build(Path(sys.argv[1]))
    BUNDLE.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    print(f"wrote {BUNDLE} ({BUNDLE.stat().st_size:,} bytes)", file=sys.stderr)

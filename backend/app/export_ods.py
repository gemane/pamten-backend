"""The spreadsheet export: one company's ownership as an OpenDocument
spreadsheet (.ods), one sheet per chapter.

Built here, not in the browser, because the browser only knows what it
loaded — the graph is capped and filtered — while the reader of a spreadsheet
wants the whole of it: every owner and subsidiary (the profile's sections,
uncapped), the whole tree below the company when asked, the timeline, and
the sources behind every assertion (the claims), which the browser never
fetches. Everything comes from the same readers the API uses, so the sheets
say what the panel says — only all of it.

Written with the standard library: an .ods is a zip of a few XML files, and
plain tables need little of the format. (odfpy would have done it, but it is
dual-licensed Apache/GPL; usable under Apache, but the project's licence guard
cannot tell an OR from an AND in classifiers and refuses to guess.) The
essentials for LibreOffice AND Excel's importer: `mimetype` first and STORED,
a manifest naming every part, `office:version` on each document, typed cells
— a number is a number, a percentage a percentage, a date a date — so the
sheet can be sorted and summed, AND a data style on every date and percentage
cell saying how to show it: typed alone, Excel showed a date as 43830. Then
what makes it a sheet rather than a CSV in a grid: columns as wide as their
longest entry, a shaded bold header row, frozen, with filter buttons.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr

MIME = "application/vnd.oasis.opendocument.spreadsheet"


# ── Cell values ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pct:
    """A percentage as a NUMBER: 7.3 → the cell holds 0.073 and shows 7.3 %."""
    value: float


@dataclass(frozen=True)
class Link:
    """A URL as a hyperlink."""
    url: str
    text: str | None = None


Cell = str | int | float | bool | Pct | Link | date | None

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_YEAR = re.compile(r"^\d{4}$")


def _cell_xml(v: Cell, header: bool = False) -> str:
    """One `<table:table-cell>` with its type, value, display text — and the
    cell style that says how to SHOW it. The type alone is not enough: with no
    date style Excel's importer showed a date as its serial number, 43830."""
    style = ' table:style-name="head"' if header else ""
    if v is None or v == "":
        return f"<table:table-cell{style}/>"
    if isinstance(v, bool):
        return (f'<table:table-cell{style} office:value-type="boolean" office:boolean-value="{str(v).lower()}">'
                f"<text:p>{str(v).lower()}</text:p></table:table-cell>")
    if isinstance(v, Pct):
        # two decimals for a stake; four where that would show "0.00 %"
        fine = 0 < abs(v.value) < 0.01
        return (f'<table:table-cell table:style-name="{"pct4" if fine else "pct"}" office:value-type="percentage" '
                f'office:value="{_plain(v.value / 100)}">'
                f"<text:p>{_num(v.value, 4 if fine else 2)} %</text:p></table:table-cell>")
    if isinstance(v, (int, float)):
        return (f'<table:table-cell{style} office:value-type="float" office:value="{_plain(v)}">'
                f"<text:p>{_num(v)}</text:p></table:table-cell>")
    if isinstance(v, date):
        return (f'<table:table-cell table:style-name="date" office:value-type="date" office:date-value="{v.isoformat()}">'
                f"<text:p>{v.isoformat()}</text:p></table:table-cell>")
    if isinstance(v, Link):
        text = escape(v.text or v.url)
        return (f'<table:table-cell{style} office:value-type="string">'
                f"<text:p><text:a xlink:type=\"simple\" xlink:href={quoteattr(v.url)}>{text}</text:a></text:p>"
                f"</table:table-cell>")
    s = str(v)
    # an ISO date in a string column is still a date to the spreadsheet; a
    # partial one ("2023-04-00", a bare year) is no date and stays text
    if _ISO_DATE.match(s):
        try:
            return _cell_xml(date.fromisoformat(s), header)
        except ValueError:
            pass
    return (f'<table:table-cell{style} office:value-type="string">'
            f"<text:p>{escape(s)}</text:p></table:table-cell>")


def _plain(v: float | int) -> str:
    """A number as the format's attribute wants it: plain decimal, never 1e-05."""
    return f"{v:.12f}".rstrip("0").rstrip(".") if isinstance(v, float) and not v.is_integer() else str(int(v))


def _num(v: float | int, decimals: int = 4) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")


# ── The document ──────────────────────────────────────────────────────────

@dataclass
class Sheet:
    name: str
    columns: list[str]
    rows: list[list[Cell]]


_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
       'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
       'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
       'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
       'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
       'xmlns:xlink="http://www.w3.org/1999/xlink" '
       'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
       'xmlns:dc="http://purl.org/dc/elements/1.1/" '
       'xmlns:number="urn:oasis:names:tc:opendocument:xmlns:datastyle:1.0" '
       'office:version="1.2"')


def _sheet_name(name: str) -> str:
    """A sheet name the format allows: no [ ] * ? : / \\, at most 31 chars
    (Excel's limit; ODS itself allows more, but the file is opened there too)."""
    return re.sub(r"[\[\]*?:/\\]", " ", name).strip()[:31] or "Sheet"


#: a column's width from its longest text: per character, plus room; bounded
_CM_PER_CHAR, _CM_PAD, _CM_MIN, _CM_MAX = 0.19, 0.5, 1.6, 12.0


def _shown(v: Cell) -> str:
    """What a cell shows, for measuring its column."""
    if v is None:
        return ""
    if isinstance(v, Pct):
        return f"{_num(v.value)} %"
    if isinstance(v, Link):
        return v.text or v.url
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def column_widths(sheet: Sheet) -> list[float]:
    """Each column wide enough for its longest entry (the header included),
    in cm, within bounds — a sheet of default-width columns with every name
    cut off reads like a CSV dropped into a grid."""
    longest = [len(c) for c in sheet.columns]
    for row in sheet.rows:
        for i, v in enumerate(row[:len(longest)]):
            longest[i] = max(longest[i], len(_shown(v)))
    return [round(min(_CM_MAX, max(_CM_MIN, n * _CM_PER_CHAR + _CM_PAD)), 2) for n in longest]


def _col_letter(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _table_xml(sheet: Sheet, index: int) -> str:
    name = _sheet_name(sheet.name)
    cols = "".join(f'<table:table-column table:style-name="co{index}-{i}" table:default-cell-style-name="Default"/>'
                   for i in range(max(1, len(sheet.columns))))
    rows = [f'<table:table-row table:style-name="rowhead">'
            f"{''.join(_cell_xml(c, header=True) for c in sheet.columns)}</table:table-row>"]
    for row in sheet.rows:
        cells = list(row) + [None] * (len(sheet.columns) - len(row))
        rows.append(f"<table:table-row>{''.join(_cell_xml(c) for c in cells)}</table:table-row>")
    return f"<table:table table:name={quoteattr(name)}>{cols}{''.join(rows)}</table:table>"


def _column_styles(sheets: list[Sheet]) -> str:
    out = []
    for t, sheet in enumerate(sheets):
        for i, w in enumerate(column_widths(sheet)):
            out.append(f'<style:style style:name="co{t}-{i}" style:family="table-column">'
                       f'<style:table-column-properties style:column-width="{w}cm"/></style:style>')
    return "".join(out)


def _filter_ranges(sheets: list[Sheet]) -> str:
    """Filter buttons on every header (`table:database-range`): a sheet of
    300 subsidiaries is there to be filtered by country or stake."""
    out = []
    for sheet in sheets:
        if not sheet.columns:
            continue
        name = _sheet_name(sheet.name)
        last = f"{_col_letter(len(sheet.columns) - 1)}{len(sheet.rows) + 1}"
        out.append(f'<table:database-range table:name={quoteattr("filter_" + name)} '
                   f'table:target-range-address={quoteattr(f"{name}.A1:{name}.{last}")} '
                   'table:display-filter-buttons="true"/>')
    return f"<table:database-ranges>{''.join(out)}</table:database-ranges>" if out else ""


def _settings_xml(sheets: list[Sheet]) -> str:
    """The header row frozen on every sheet (LibreOffice reads this; Excel's
    importer ignores settings, and loses nothing by it)."""
    per_table = "".join(
        f'<config:config-item-map-entry config:name={quoteattr(_sheet_name(s.name))}>'
        '<config:config-item config:name="HorizontalSplitMode" config:type="short">0</config:config-item>'
        '<config:config-item config:name="VerticalSplitMode" config:type="short">2</config:config-item>'
        '<config:config-item config:name="HorizontalSplitPosition" config:type="int">0</config:config-item>'
        '<config:config-item config:name="VerticalSplitPosition" config:type="int">1</config:config-item>'
        '<config:config-item config:name="ActiveSplitRange" config:type="short">2</config:config-item>'
        '<config:config-item config:name="PositionTop" config:type="int">0</config:config-item>'
        '<config:config-item config:name="PositionBottom" config:type="int">1</config:config-item>'
        "</config:config-item-map-entry>"
        for s in sheets)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-settings xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0" office:version="1.2">'
        "<office:settings><config:config-item-set config:name=\"ooo:view-settings\">"
        '<config:config-item-map-indexed config:name="Views"><config:config-item-map-entry>'
        '<config:config-item config:name="ViewId" config:type="string">view1</config:config-item>'
        f'<config:config-item-map-named config:name="Tables">{per_table}</config:config-item-map-named>'
        "</config:config-item-map-entry></config:config-item-map-indexed>"
        "</config:config-item-set></office:settings></office:document-settings>"
    )


def ods_bytes(sheets: Iterable[Sheet], title: str = "") -> bytes:
    """The .ods file for these sheets."""
    sheets = list(sheets)
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<office:document-content {_NS}>"
        "<office:automatic-styles>"
        # how a date and a percentage are SHOWN: ISO date; two or four decimals
        '<number:date-style style:name="N-date"><number:year number:style="long"/><number:text>-</number:text>'
        '<number:month number:style="long"/><number:text>-</number:text><number:day number:style="long"/></number:date-style>'
        '<number:percentage-style style:name="N-pct"><number:number number:decimal-places="2" number:min-integer-digits="1"/>'
        "<number:text> %</number:text></number:percentage-style>"
        '<number:percentage-style style:name="N-pct4"><number:number number:decimal-places="4" number:min-integer-digits="1"/>'
        "<number:text> %</number:text></number:percentage-style>"
        '<style:style style:name="date" style:family="table-cell" style:data-style-name="N-date"/>'
        '<style:style style:name="pct" style:family="table-cell" style:data-style-name="N-pct"/>'
        '<style:style style:name="pct4" style:family="table-cell" style:data-style-name="N-pct4"/>'
        '<style:style style:name="head" style:family="table-cell">'
        '<style:table-cell-properties fo:background-color="#e8edf5" fo:border-bottom="0.5pt solid #9aa5b8"/>'
        '<style:text-properties fo:font-weight="bold"/></style:style>'
        '<style:style style:name="rowhead" style:family="table-row">'
        '<style:table-row-properties style:row-height="0.6cm" style:use-optimal-row-height="false"/></style:style>'
        + _column_styles(sheets)
        + "</office:automatic-styles>"
        "<office:body><office:spreadsheet>"
        + "".join(_table_xml(s, i) for i, s in enumerate(sheets))
        + _filter_ranges(sheets)
        + "</office:spreadsheet></office:body></office:document-content>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<office:document-styles {_NS}><office:styles>"
        '<style:default-style style:family="table-cell"><style:text-properties fo:font-size="10pt"/></style:default-style>'
        "</office:styles></office:document-styles>"
    )
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    meta = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<office:document-meta {_NS}><office:meta>"
        f"<dc:title>{escape(title)}</dc:title><meta:generator>Owlgraph</meta:generator>"
        f"<meta:creation-date>{now}</meta:creation-date>"
        "</office:meta></office:document-meta>"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
        f'<manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="{MIME}"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="settings.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # the mimetype first and uncompressed: that is how a reader sniffs the format
        z.writestr(zipfile.ZipInfo("mimetype"), MIME, compress_type=zipfile.ZIP_STORED)
        for name, data in (("content.xml", content), ("styles.xml", styles), ("meta.xml", meta),
                           ("settings.xml", _settings_xml(sheets)), ("META-INF/manifest.xml", manifest)):
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


# ── The company's workbook ────────────────────────────────────────────────

#: every row of every section: the profile's cap is for a page, not a file
EXPORT_SECTION_LIMIT = 100_000


@dataclass(frozen=True)
class ExportOptions:
    as_of: str | None = None
    all_levels: bool = False
    #: stated stakes below this (percent) are left out; unstated ones stay
    min_stake: float = 0.0
    #: `> min_stake` instead of `>= min_stake`
    min_stake_exclusive: bool = False
    #: the live graph, for the Overview sheet
    link: str | None = None


def keeps_stake(stake: Any, opt: ExportOptions) -> bool:
    """The graph's stake-filter rule: an unstated stake is never filtered."""
    if not isinstance(stake, (int, float)) or isinstance(stake, bool):
        return True
    return stake > opt.min_stake if opt.min_stake_exclusive else stake >= opt.min_stake


def _pct(v: Any) -> Cell:
    return Pct(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _name(node: dict) -> str:
    return node.get("name") or node.get("full_name") or node.get("id") or ""


def _type(node: dict) -> str:
    return "person" if "full_name" in node and "name" not in node else (node.get("type") or "")


def _rel_cells(rel: dict, sources: dict[str, str]) -> list[Cell]:
    """The columns every holding shares: type, stake, votes, dates, source."""
    return [rel.get("ownership_type"), _pct(rel.get("stake_percent")), _pct(rel.get("voting_power_pct")),
            rel.get("since"), rel.get("since_basis"), rel.get("until"), rel.get("until_reason"),
            rel.get("direct_or_indirect"), _source(rel.get("source_id"), sources),
            Link(rel["source_url"]) if rel.get("source_url") else None, rel.get("source_date"),
            ", ".join(rel.get("asserted_by") or []) or None]


def _source(source_id: Any, sources: dict[str, str]) -> Cell:
    """The source's name; its id when the Source node is gone."""
    return sources.get(source_id, source_id) if source_id else None


def source_names() -> dict[str, str]:
    """Every Source node's name by id — a few dozen rows, read once per export."""
    from app.database import db
    with db.get_session() as session:
        return {r.get("id"): r.get("name") for r in session.run("MATCH (s:Source) RETURN s.id AS id, s.name AS name")
                if r.get("id") and r.get("name")}


REL_COLUMNS = ["Ownership type", "Stake", "Voting power", "Since", "Since basis", "Until", "Until reason",
               "Direct / indirect", "Source", "Source URL", "Source date", "Also asserted by"]
ENTITY_COLUMNS = ["Type", "Country", "LEI", "Register id", "Companies House", "SEC CIK", "Wikidata"]


def _entity_cells(node: dict) -> list[Cell]:
    return [node.get("type"), node.get("country"), node.get("lei_id"), node.get("register_id"),
            node.get("companies_house_id"), node.get("sec_cik"), node.get("wikidata_id")]


def build_workbook(entity_id: str, opt: ExportOptions) -> tuple[str, bytes]:
    """`(filename, bytes)` for this company. Raises the profile's HTTPException
    (404) when the company does not exist."""
    # the API's own readers: the sheets say what the panel says
    from app.routers.search import get_full_profile
    from app.routers.relationships import (subsidiary_tree_of, ownership_history_of,
                                           SUBTREE_MAX_NODES, HISTORY_MAX_LIMIT)
    from app.routers.sources import get_sources_for_entity
    from app.claims import claims_for

    profile = get_full_profile(entity_id, limit=EXPORT_SECTION_LIMIT, as_of=opt.as_of)
    entity = profile["entity"]
    entity_id = entity.get("id") or entity_id   # a merged id was followed
    name = _name(entity)
    today = datetime.now(timezone.utc).date()
    sources = source_names()

    owners = [o for o in profile["owners"] if keeps_stake(o["relationship"].get("stake_percent"), opt)]
    owner_rows = [[_name(o["owner"]), _type(o["owner"]), o["owner"].get("country"), *_rel_cells(o["relationship"], sources)]
                  for o in owners]

    truncated = False
    if opt.all_levels:
        tree = subsidiary_tree_of(entity_id, SUBTREE_MAX_NODES, opt.as_of) or {"nodes": [], "edges": [], "truncated": False}
        truncated = bool(tree.get("truncated"))
        by_id = {n["entity"]["id"]: n["entity"] for n in tree["nodes"]}
        by_id[entity_id] = entity
        placing = {(e["from_id"], e["to_id"]): e["relationship"] for e in tree["edges"]}
        sub_rows = []
        for n in sorted(tree["nodes"], key=lambda n: (n["depth"], _name(n["entity"]).lower())):
            rel = placing.get((n["parent_id"], n["entity"]["id"]), {})
            if not keeps_stake(rel.get("stake_percent"), opt):
                continue
            sub_rows.append([n["depth"], _name(by_id.get(n["parent_id"], {})), _name(n["entity"]),
                             *_entity_cells(n["entity"]), *_rel_cells(rel, sources)])
        sub_columns = ["Level", "Parent", "Company", *ENTITY_COLUMNS, *REL_COLUMNS]
    else:
        subs = [s for s in profile["subsidiaries"] if keeps_stake(s["relationship"].get("stake_percent"), opt)]
        sub_rows = [[_name(s["entity"]), *_entity_cells(s["entity"]), *_rel_cells(s["relationship"], sources)] for s in subs]
        sub_columns = ["Company", *ENTITY_COLUMNS, *REL_COLUMNS]

    role_rows = [[_name(x["person"]), x["role"].get("role"), x["person"].get("nationality"),
                  x["role"].get("since"), x["role"].get("until"), _source(x["role"].get("source_id"), sources),
                  Link(x["role"]["source_url"]) if x["role"].get("source_url") else None,
                  x["role"].get("source_date"), ", ".join(x["role"].get("asserted_by") or []) or None]
                 for x in profile["executives"]]

    events, _ = ownership_history_of(entity_id, HISTORY_MAX_LIMIT)
    kind_label = {"ownership_in": "Owned by", "ownership_out": "Owns", "role": "Role"}
    timeline_rows = [[kind_label.get(e["kind"], e["kind"]), _name(e["party"]), e.get("role"),
                      e.get("ownership_type"), _pct(e.get("stake_percent")), e.get("since"),
                      e.get("since_basis"), e.get("until"), bool(e.get("active"))]
                     for e in events]

    source_rows = [[s.get("name"), s.get("type"), s.get("credibility_score"),
                    Link(s["url"]) if s.get("url") else None, s.get("source_date"), s.get("last_scraped_at"),
                    s.get("filing_type"), s.get("read_from")]
                   for s in get_sources_for_entity(entity_id)]

    claims = claims_for(to_id=entity_id) + claims_for(from_id=entity_id, kind="owns")
    names = {entity_id: name}
    for o in profile["owners"]:
        names[o["owner"].get("id")] = _name(o["owner"])
    for s in profile["subsidiaries"]:
        names[s["entity"].get("id")] = _name(s["entity"])
    for x in profile["executives"]:
        names[x["person"].get("id")] = _name(x["person"])
    claim_rows = [[c.get("kind"), names.get(c.get("from_id"), c.get("from_id")), names.get(c.get("to_id"), c.get("to_id")),
                   c.get("role"), c.get("ownership_type"), _pct(c.get("stake_percent")), _pct(c.get("voting_power_pct")),
                   c.get("since"), c.get("since_basis"), c.get("until"), _source(c.get("source_id"), sources), c.get("filing_type"),
                   Link(c["source_url"]) if c.get("source_url") else None, c.get("source_date"),
                   c.get("credibility_score"), c.get("first_seen_at"), c.get("last_seen_at"),
                   c.get("read_from")]
                  for c in claims]

    counts = profile.get("counts") or {}
    stake_label = ("any" if not opt.min_stake else
                   f"{'>' if opt.min_stake_exclusive else '>='} {_num(opt.min_stake)} %")
    overview_rows: list[list[Cell]] = [
        ["Name", name], ["Type", entity.get("type")], ["Country", entity.get("country")],
        ["Legal form", entity.get("legal_form")], ["Founded", entity.get("founded")],
        ["Revenue", entity.get("revenue")], ["Employees", entity.get("employees")],
        ["Headquarters", ", ".join(p for p in (entity.get("hq_city"), entity.get("hq_country")) if p) or None],
        ["Address", entity.get("address")],
        ["Website", Link(entity["website"]) if entity.get("website") else None],
        ["LEI", entity.get("lei_id")], ["Register id", entity.get("register_id")],
        ["Registered at", entity.get("registration_authority")],
        ["Companies House", entity.get("companies_house_id")], ["SEC CIK", entity.get("sec_cik")],
        ["Wikidata", entity.get("wikidata_id")], ["Description", entity.get("description")],
        [None, None],
        ["Owners", counts.get("owners")], ["Subsidiaries (direct)", counts.get("subsidiaries")],
        ["Executives", counts.get("executives")],
        [None, None],
        ["Exported", today], ["As of", opt.as_of or "present"],
        ["Subsidiaries", "all levels" + (" (capped, more exist)" if truncated else "") if opt.all_levels else "direct"],
        ["Minimum stake", stake_label],
        ["Owners listed", len(owner_rows)], ["Subsidiaries listed", len(sub_rows)],
        ["Live graph", Link(opt.link) if opt.link else None],
        ["Source", "Owlgraph — data under ODbL v1.0"],
    ]

    sheets = [
        Sheet("Overview", ["Field", "Value"], overview_rows),
        Sheet("Owners", ["Owner", "Type", "Country", *REL_COLUMNS], owner_rows),
        Sheet("Subsidiaries", sub_columns, sub_rows),
        Sheet("Roles", ["Person", "Role", "Nationality", "Since", "Until", "Source", "Source URL", "Source date",
                        "Also asserted by"], role_rows),
        Sheet("Timeline", ["Kind", "Party", "Role", "Ownership type", "Stake", "Since", "Since basis", "Until",
                           "Current"], timeline_rows),
        Sheet("Sources", ["Source", "Type", "Credibility", "URL", "Record date", "Last read", "Filing"], source_rows),
        Sheet("Claims", ["Kind", "From", "To", "Role", "Ownership type", "Stake", "Voting power", "Since",
                         "Since basis", "Until", "Source", "Filing", "Source URL", "Source date", "Credibility",
                         "First seen", "Last seen"], claim_rows),
    ]
    suffix = f" as of {opt.as_of[:4]}" if opt.as_of and _ISO_DATE.match(opt.as_of) else ""
    filename = re.sub(r"[^\w .()&-]+", "_", name).strip() or "company"
    return f"{filename}{suffix}.ods", ods_bytes(sheets, title=f"{name} — Owlgraph")

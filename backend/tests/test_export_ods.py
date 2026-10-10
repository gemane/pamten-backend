"""The .ods writer and the company workbook, without a database: the file's
structure (what LibreOffice and Excel sniff and parse), the typing of cells,
and the sheets built from the profile, the tree, the history and the claims —
those readers replaced by stand-ins."""
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import export_ods as mod
from app.export_ods import (MIME, ExportOptions, Link, Pct, Sheet, build_workbook,
                            column_widths, keeps_stake, ods_bytes)
from app.routers.relationships import SUBTREE_MAX_NODES

NS = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
      "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
      "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
      "xlink": "http://www.w3.org/1999/xlink"}


def sheets_of(data: bytes) -> dict[str, list[list[tuple[str | None, str]]]]:
    """Every sheet as rows of (value-type, text) — read back with the
    standard parser, independently of the writer."""
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("content.xml"))
    out = {}
    for tbl in root.iter(f"{{{NS['table']}}}table"):
        rows = []
        for row in tbl.iter(f"{{{NS['table']}}}table-row"):
            rows.append([(c.get(f"{{{NS['office']}}}value-type"), "".join(c.itertext()))
                         for c in row.iter(f"{{{NS['table']}}}table-cell")])
        out[tbl.get(f"{{{NS['table']}}}name")] = rows
    return out


# ── The file ──────────────────────────────────────────────────────────────

class TestTheFile:
    def test_the_mimetype_comes_first_uncompressed_and_every_part_is_in_the_manifest(self):
        z = zipfile.ZipFile(io.BytesIO(ods_bytes([Sheet("A", ["x"], [])])))
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == MIME.encode()
        manifest = z.read("META-INF/manifest.xml").decode()
        for part in ("content.xml", "styles.xml", "meta.xml", "settings.xml"):
            assert part in names and f'manifest:full-path="{part}"' in manifest
        assert f'manifest:full-path="/" manifest:version="1.2" manifest:media-type="{MIME}"' in manifest

    def test_every_part_is_well_formed_xml_with_the_format_version(self):
        z = zipfile.ZipFile(io.BytesIO(ods_bytes([Sheet("A", ["x"], [["<&>"]])], title="T & co")))
        for part in ("content.xml", "styles.xml", "meta.xml", "settings.xml", "META-INF/manifest.xml"):
            root = ET.fromstring(z.read(part))
            if part != "META-INF/manifest.xml":
                assert root.get(f"{{{NS['office']}}}version") == "1.2"

    def test_one_table_per_sheet_in_order_with_a_bold_header_row(self):
        data = ods_bytes([Sheet("Owners", ["Owner", "Stake"], [["A", Pct(7.3)]]), Sheet("Roles", ["Person"], [])])
        s = sheets_of(data)
        assert list(s) == ["Owners", "Roles"]
        assert s["Owners"][0] == [("string", "Owner"), ("string", "Stake")]
        assert s["Roles"] == [[("string", "Person")]]
        root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("content.xml"))
        head = root.find(f".//{{{NS['table']}}}table-row/{{{NS['table']}}}table-cell")
        assert head.get(f"{{{NS['table']}}}style-name") == "head"
        assert 'style:name="head"' in zipfile.ZipFile(io.BytesIO(data)).read("content.xml").decode()
        assert 'fo:font-weight="bold"' in zipfile.ZipFile(io.BytesIO(data)).read("content.xml").decode()

    def test_the_header_is_shaded_and_bold_the_row_frozen_with_filter_buttons(self):
        data = ods_bytes([Sheet("Owners", ["Owner", "Stake"], [["A", Pct(7.3)], ["B", Pct(1)]]), Sheet("Roles", ["Person"], [])])
        z = zipfile.ZipFile(io.BytesIO(data))
        content = z.read("content.xml").decode()
        assert 'fo:background-color="#e8edf5"' in content and 'fo:font-weight="bold"' in content
        # filter buttons over each sheet's whole range
        assert 'table:target-range-address="Owners.A1:Owners.B3" table:display-filter-buttons="true"' in content
        assert 'table:target-range-address="Roles.A1:Roles.A1" table:display-filter-buttons="true"' in content
        settings = z.read("settings.xml").decode()
        for name in ("Owners", "Roles"):
            assert f'config:name="{name}"' in settings
        assert settings.count('config:name="VerticalSplitPosition" config:type="int">1<') == 2

    def test_a_sheet_name_is_made_legal_for_both_readers(self):
        s = sheets_of(ods_bytes([Sheet("A/B:C*D?[E]\\F", ["x"], []), Sheet("x" * 40, ["x"], []), Sheet("  ", ["x"], [])]))
        assert list(s) == ["A B C D  E  F", "x" * 31, "Sheet"]


class TestColumnWidths:
    def test_each_column_as_wide_as_its_longest_entry_within_bounds(self):
        sheet = Sheet("S", ["Owner", "Ownership type", "x"], [["A very long company name indeed", Pct(7.3), None],
                                                              ["B", Pct(100), "y" * 400]])
        w = column_widths(sheet)
        assert w[0] == round(len("A very long company name indeed") * 0.19 + 0.5, 2)
        assert w[1] == round(len("Ownership type") * 0.19 + 0.5, 2)   # the header is the longest
        assert w[2] == 12.0                                            # capped
        assert column_widths(Sheet("S", ["x"], []))[0] == 1.6        # the floor

    def test_the_widths_are_written_as_column_styles(self):
        data = ods_bytes([Sheet("A", ["Owner"], [["Some company"]]), Sheet("B", ["x", "yy"], [])])
        content = zipfile.ZipFile(io.BytesIO(data)).read("content.xml").decode()
        assert '<style:style style:name="co0-0" style:family="table-column">' in content
        assert f'style:column-width="{round(12 * 0.19 + 0.5, 2)}cm"' in content
        assert '<table:table-column table:style-name="co1-1"' in content


class TestCells:
    def cell(self, v):
        return sheets_of(ods_bytes([Sheet("S", ["c"], [[v]])]))["S"][1][0]

    def cell_style(self, v):
        root = ET.fromstring(zipfile.ZipFile(io.BytesIO(ods_bytes([Sheet("S", ["c"], [[v]])]))).read("content.xml"))
        cells = list(root.iter(f"{{{NS['table']}}}table-cell"))
        return cells[1].get(f"{{{NS['table']}}}style-name")

    def test_a_date_and_a_percentage_carry_a_display_style_the_rest_none(self):
        # typed alone, Excel's importer showed a date as its serial number
        assert self.cell_style(date(2019, 12, 31)) == "date"
        assert self.cell_style("2019-12-31") == "date"
        assert self.cell_style(Pct(7.3)) == "pct"
        assert self.cell_style(Pct(0.0064)) == "pct4"                # two decimals would show 0.00 %
        assert self.cell_style(Pct(0)) == "pct"
        assert self.cell_style(42) is None and self.cell_style("text") is None
        content = zipfile.ZipFile(io.BytesIO(ods_bytes([Sheet("S", ["c"], [[Pct(1)]])]))).read("content.xml").decode()
        assert '<style:style style:name="date" style:family="table-cell" style:data-style-name="N-date"/>' in content
        assert '<number:date-style style:name="N-date">' in content
        assert '<number:percentage-style style:name="N-pct"><number:number number:decimal-places="2"' in content
        assert '<number:percentage-style style:name="N-pct4"><number:number number:decimal-places="4"' in content

    def test_a_number_is_a_number_a_percentage_a_percentage_a_date_a_date(self):
        assert self.cell(42) == ("float", "42")
        assert self.cell(2.5) == ("float", "2.5")
        assert self.cell(Pct(7.3)) == ("percentage", "7.3 %")
        assert self.cell(Pct(100)) == ("percentage", "100 %")
        assert self.cell(Pct(0.0064)) == ("percentage", "0.0064 %")
        assert self.cell(Pct(7.123456)) == ("percentage", "7.12 %")
        assert self.cell(date(2019, 12, 31)) == ("date", "2019-12-31")
        assert self.cell(True) == ("boolean", "true")

    def test_the_stored_values_are_plain_decimals_never_scientific(self):
        xml = zipfile.ZipFile(io.BytesIO(ods_bytes([Sheet("S", ["c"], [[Pct(0.0001), 0.00001]])]))).read("content.xml").decode()
        assert 'office:value="0.000001"' in xml and 'office:value="0.00001"' in xml
        assert "e-0" not in xml

    def test_an_iso_date_in_a_string_is_a_date_a_partial_one_stays_text(self):
        assert self.cell("2019-12-31") == ("date", "2019-12-31")
        assert self.cell("2023-04-00") == ("string", "2023-04-00")
        assert self.cell("2019") == ("string", "2019")
        assert self.cell("2019-02-30") == ("string", "2019-02-30")            # not a real day
        assert self.cell("2026-09-20T18:46:41Z") == ("string", "2026-09-20T18:46:41Z")

    def test_a_link_is_a_hyperlink_and_text_is_escaped(self):
        data = ods_bytes([Sheet("S", ["c"], [[Link("https://example.com/a?b=1&c=2", "the record")], ["<b> & co"]])])
        xml = zipfile.ZipFile(io.BytesIO(data)).read("content.xml").decode()
        assert 'xlink:href="https://example.com/a?b=1&amp;c=2">the record</text:a>' in xml
        assert "&lt;b&gt; &amp; co" in xml
        assert self.cell(Link("https://example.com")) == ("string", "https://example.com")

    def test_nothing_is_an_empty_cell_and_a_short_row_is_padded(self):
        rows = sheets_of(ods_bytes([Sheet("S", ["a", "b", "c"], [[None, ""], ["x"]])]))["S"]
        assert rows[1] == [(None, ""), (None, ""), (None, "")]
        assert rows[2] == [("string", "x"), (None, ""), (None, "")]


class TestKeepsStake:
    def test_the_graphs_rule_unstated_stays_stated_is_compared(self):
        at_least_5 = ExportOptions(min_stake=5)
        assert keeps_stake(None, at_least_5) and keeps_stake("n/a", at_least_5)
        assert keeps_stake(5.0, at_least_5) and not keeps_stake(4.99, at_least_5)
        above_50 = ExportOptions(min_stake=50, min_stake_exclusive=True)
        assert not keeps_stake(50, above_50) and keeps_stake(50.1, above_50)
        assert keeps_stake(0, ExportOptions())


# ── The workbook ──────────────────────────────────────────────────────────

PROFILE = {
    "entity": {"id": "ms", "name": "MICROSOFT CORPORATION", "type": "company", "country": "US",
               "founded": 1975, "revenue": 331800000000, "employees": 228000, "hq_city": "Redmond",
               "hq_country": "US", "website": "https://example.com", "lei_id": "LEI1"},
    "counts": {"owners": 2, "subsidiaries": 2, "executives": 1},
    "owners": [
        {"owner": {"id": "br", "name": "BlackRock", "type": "fund", "country": "US"},
         "relationship": {"stake_percent": 7.3, "ownership_type": "minority", "since": "2024-02-13",
                          "source_id": "sec", "source_url": "https://example.com/13g", "asserted_by": ["SEC EDGAR"]}},
        {"owner": {"id": "bg", "full_name": "Bill Gates"},
         "relationship": {"stake_percent": 0.5, "ownership_type": "minority", "source_id": "gone"}},
    ],
    "subsidiaries": [
        {"entity": {"id": "li", "name": "LinkedIn", "type": "company", "country": "US"},
         "relationship": {"stake_percent": 100, "ownership_type": "full", "source_id": "sec", "descendants": 1}},
        {"entity": {"id": "gh", "name": "GitHub", "type": "company", "country": "US"},
         "relationship": {"ownership_type": "unknown", "source_id": "sec", "descendants": 0}},   # no stake stated
    ],
    "executives": [
        {"person": {"id": "sn", "full_name": "Satya Nadella", "nationality": "US"},
         "role": {"role": "CEO", "since": "2014-02-04", "source_id": "sec"}},
    ],
}
TREE = {"root_id": "ms", "truncated": True, "total": 10,
        "nodes": [{"entity": {"id": "li", "name": "LinkedIn", "type": "company"}, "parent_id": "ms", "depth": 1},
                  {"entity": {"id": "lii", "name": "LinkedIn Ireland", "type": "company"}, "parent_id": "li", "depth": 2},
                  {"entity": {"id": "gh", "name": "GitHub", "type": "company"}, "parent_id": "ms", "depth": 1}],
        "edges": [{"from_id": "ms", "to_id": "li", "depth": 1, "relationship": {"stake_percent": 100, "descendants": 1}},
                  {"from_id": "li", "to_id": "lii", "depth": 2, "relationship": {"stake_percent": 100, "descendants": 0}},
                  {"from_id": "ms", "to_id": "gh", "depth": 1, "relationship": {"stake_percent": 0.5, "descendants": 0}}]}
EVENTS = [{"kind": "ownership_in", "party": {"id": "br", "name": "BlackRock"}, "since": "2024-02-13",
           "stake_percent": 7.3, "ownership_type": "minority", "active": True},
          {"kind": "role", "party": {"id": "sn", "full_name": "Satya Nadella"}, "role": "CEO",
           "since": "2014-02-04", "until": None, "active": True}]
SOURCES = [{"name": "SEC EDGAR", "type": "register", "credibility_score": 98, "url": "https://example.com/13g",
            "source_date": "2024-02-13", "last_scraped_at": "2026-09-11T12:48:35Z", "filing_type": "13G"}]
CLAIMS_TO = [{"kind": "owns", "from_id": "br", "to_id": "ms", "stake_percent": 7.3, "source_id": "sec",
              "source_url": "https://example.com/13g", "first_seen_at": "2026-01-01T00:00:00Z"}]
CLAIMS_FROM = [{"kind": "owns", "from_id": "ms", "to_id": "li", "stake_percent": 100, "source_id": "sec"}]


@pytest.fixture
def readers(monkeypatch):
    """The API's readers replaced; each records what it was asked."""
    calls = {}
    import app.routers.search as search
    import app.routers.relationships as rels
    import app.routers.sources as sources
    import app.claims as claims

    def profile(entity_id, limit=None, as_of=None):
        calls["profile"] = (entity_id, limit, as_of)
        return PROFILE
    def tree(entity_id, max_nodes=None, as_of=None):
        calls["tree"] = (entity_id, max_nodes, as_of)
        return TREE
    def history(entity_id, limit=None):
        calls["history"] = (entity_id, limit)
        return EVENTS, False
    def claims_for(from_id=None, to_id=None, kind=None):
        return CLAIMS_TO if to_id else CLAIMS_FROM
    monkeypatch.setattr(search, "get_full_profile", profile)
    monkeypatch.setattr(rels, "subsidiary_tree_of", tree)
    monkeypatch.setattr(rels, "ownership_history_of", history)
    monkeypatch.setattr(sources, "get_sources_for_entity", lambda eid: SOURCES)
    monkeypatch.setattr(claims, "claims_for", claims_for)
    monkeypatch.setattr(mod, "source_names", lambda: {"sec": "SEC EDGAR"})
    return calls


def column(rows, name):
    idx = [c[1] for c in rows[0]].index(name)
    return [r[idx] for r in rows[1:]]


class TestTheWorkbook:
    def test_seven_sheets_from_the_uncapped_readers(self, readers):
        filename, data = build_workbook("ms", ExportOptions())
        s = sheets_of(data)
        assert list(s) == ["Overview", "Owners", "Subsidiaries", "Roles", "Timeline", "Sources", "Claims"]
        assert filename == "MICROSOFT CORPORATION.ods"
        assert readers["profile"] == ("ms", mod.EXPORT_SECTION_LIMIT, None)
        assert readers["history"][1] >= 2_000                   # the history's own maximum
        assert "tree" not in readers                            # direct: the profile's list

    def test_the_overview_names_the_company_the_export_and_the_filters(self, readers):
        _, data = build_workbook("ms", ExportOptions(link="https://owlgraph.example/#graph/e/ms", min_stake=1))
        rows = {r[0][1]: r[1] for r in sheets_of(data)["Overview"][1:] if r[0][1]}
        assert rows["Name"] == ("string", "MICROSOFT CORPORATION")
        assert rows["Founded"] == ("float", "1975")
        assert rows["Employees"] == ("float", "228000")
        assert rows["Headquarters"] == ("string", "Redmond, US")
        assert rows["Website"] == ("string", "https://example.com")
        assert rows["Owners"] == ("float", "2")
        assert rows["Exported"][0] == "date"
        assert rows["As of"] == ("string", "present")
        assert rows["Subsidiaries"] == ("string", "direct")
        assert rows["Minimum stake"] == ("string", ">= 1 %")
        assert rows["Owners listed"] == ("float", "1")             # Gates at 0.5 % is out
        assert rows["Live graph"] == ("string", "https://owlgraph.example/#graph/e/ms")

    def test_owners_typed_with_the_source_named_and_the_stake_filter_applied(self, readers):
        _, data = build_workbook("ms", ExportOptions())
        rows = sheets_of(data)["Owners"]
        assert column(rows, "Owner") == [("string", "BlackRock"), ("string", "Bill Gates")]
        assert column(rows, "Type") == [("string", "fund"), ("string", "person")]
        assert column(rows, "Stake") == [("percentage", "7.3 %"), ("percentage", "0.5 %")]
        assert column(rows, "Since") == [("date", "2024-02-13"), (None, "")]
        assert column(rows, "Source") == [("string", "SEC EDGAR"), ("string", "gone")]   # unknown id: the id
        assert column(rows, "Source URL") == [("string", "https://example.com/13g"), (None, "")]
        assert column(rows, "Also asserted by") == [("string", "SEC EDGAR"), (None, "")]
        _, data = build_workbook("ms", ExportOptions(min_stake=5))
        assert column(sheets_of(data)["Owners"], "Owner") == [("string", "BlackRock")]

    def test_direct_subsidiaries_keep_an_unstated_stake_under_any_filter(self, readers):
        _, data = build_workbook("ms", ExportOptions(min_stake=50))
        rows = sheets_of(data)["Subsidiaries"]
        assert column(rows, "Company") == [("string", "LinkedIn"), ("string", "GitHub")]
        assert rows[0][0] == ("string", "Company")                 # no Level/Parent columns

    def test_both_subsidiary_sheets_say_how_many_companies_sit_below_each(self, readers):
        _, data = build_workbook("ms", ExportOptions())
        rows = sheets_of(data)["Subsidiaries"]
        assert column(rows, "Company") == [("string", "LinkedIn"), ("string", "GitHub")]
        assert column(rows, "Companies below") == [("float", "1"), ("float", "0")]
        _, data = build_workbook("ms", ExportOptions(all_levels=True))
        rows = sheets_of(data)["Subsidiaries"]
        assert column(rows, "Company") == [("string", "GitHub"), ("string", "LinkedIn"), ("string", "LinkedIn Ireland")]
        assert column(rows, "Companies below") == [("float", "0"), ("float", "1"), ("float", "0")]

    def test_all_levels_lists_the_tree_with_level_and_parent_and_says_when_capped(self, readers):
        _, data = build_workbook("ms", ExportOptions(all_levels=True, as_of="2019-12-31", min_stake=1))
        assert readers["tree"] == ("ms", SUBTREE_MAX_NODES, "2019-12-31")
        assert readers["profile"][2] == "2019-12-31"
        s = sheets_of(data)
        rows = s["Subsidiaries"]
        assert [c[1] for c in rows[0]][:3] == ["Level", "Parent", "Company"]
        # GitHub's 0.5 % placing holding is below the filter; LinkedIn Ireland sits under LinkedIn
        assert [(r[0][1], r[1][1], r[2][1]) for r in rows[1:]] == [("1", "MICROSOFT CORPORATION", "LinkedIn"),
                                                                   ("2", "LinkedIn", "LinkedIn Ireland")]
        overview = {r[0][1]: r[1][1] for r in s["Overview"][1:] if r[0][1]}
        assert overview["Subsidiaries"] == "all levels (the first 2 of 10)"     # the rows listed, of the whole tree
        assert overview["As of"] == "2019-12-31"

    def test_a_capped_tree_whose_count_also_gave_up_still_says_more_exist(self, readers, monkeypatch):
        import app.routers.relationships as rels
        monkeypatch.setattr(rels, "subsidiary_tree_of", lambda *a, **k: {**TREE, "total": None})
        _, data = build_workbook("ms", ExportOptions(all_levels=True))
        overview = {r[0][1]: r[1][1] for r in sheets_of(data)["Overview"][1:] if r[0][1]}
        assert overview["Subsidiaries"] == "all levels (capped, more exist)"

    def test_roles_timeline_sources_and_claims(self, readers):
        _, data = build_workbook("ms", ExportOptions())
        s = sheets_of(data)
        assert s["Roles"][1][:4] == [("string", "Satya Nadella"), ("string", "CEO"), ("string", "US"), ("date", "2014-02-04")]
        assert column(s["Timeline"], "Kind") == [("string", "Owned by"), ("string", "Role")]
        assert column(s["Timeline"], "Current") == [("boolean", "true"), ("boolean", "true")]
        assert s["Sources"][1][:3] == [("string", "SEC EDGAR"), ("string", "register"), ("float", "98")]
        # claims about the company and the company's own holdings, names resolved
        assert [(r[1][1], r[2][1]) for r in s["Claims"][1:]] == [("BlackRock", "MICROSOFT CORPORATION"),
                                                                 ("MICROSOFT CORPORATION", "LinkedIn")]
        assert column(s["Claims"], "Source") == [("string", "SEC EDGAR"), ("string", "SEC EDGAR")]
        assert column(s["Claims"], "First seen") == [("string", "2026-01-01T00:00:00Z"), (None, "")]

    def test_the_filename_is_safe_and_names_the_year(self, readers):
        PROFILE["entity"]["name"] = 'A/B "Co" <x>'
        try:
            filename, _ = build_workbook("ms", ExportOptions(as_of="2019-12-31"))
        finally:
            PROFILE["entity"]["name"] = "MICROSOFT CORPORATION"
        assert filename == "A_B _Co_ _x_ as of 2019.ods"


# ── The route ─────────────────────────────────────────────────────────────

class TestTheRoute:
    @pytest.fixture
    def client(self, monkeypatch):
        import app.routers.export as route
        self.calls = []
        def fake(entity_id, opt):
            self.calls.append((entity_id, opt))
            return "Näme as of 2019.ods", b"PK-bytes"
        monkeypatch.setattr(route, "build_workbook", fake)
        from app.main import app
        return TestClient(app)

    def test_the_file_comes_back_as_an_attachment_under_v1_and_bare(self, client):
        for prefix in ("/v1", ""):
            r = client.get(f"{prefix}/export/entity/lei:X?as_of=2019-12-31&all_levels=true&min_stake=5"
                           "&min_stake_exclusive=true&link=https://owlgraph.example/%23graph")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith(MIME)
            assert r.headers["content-disposition"] == (
                'attachment; filename="N?me as of 2019.ods"; filename*=UTF-8\'\'N%C3%A4me%20as%20of%202019.ods')
            assert r.headers["cache-control"] == "no-store"
            assert r.content == b"PK-bytes"
        assert self.calls[-1] == ("lei:X", ExportOptions(as_of="2019-12-31", all_levels=True, min_stake=5.0,
                                                          min_stake_exclusive=True, link="https://owlgraph.example/#graph"))

    def test_the_defaults_are_the_present_direct_and_unfiltered(self, client):
        assert client.get("/v1/export/entity/lei:X").status_code == 200
        assert self.calls[-1][1] == ExportOptions()

    @pytest.mark.parametrize("query", ["as_of=2019", "as_of=2019-12-31T00:00", "min_stake=101", "min_stake=-1",
                                       "link=ftp://x", "link=javascript:alert(1)"])
    def test_bad_parameters_are_refused(self, client, query):
        assert client.get(f"/v1/export/entity/lei:X?{query}").status_code == 422
        assert self.calls == []

    def test_an_unknown_company_is_404(self, monkeypatch):
        import app.routers.export as route
        from fastapi import HTTPException
        def missing(entity_id, opt):
            raise HTTPException(status_code=404, detail="Entity not found")
        monkeypatch.setattr(route, "build_workbook", missing)
        from app.main import app
        assert TestClient(app).get("/v1/export/entity/nope").status_code == 404


class TestCors:
    def test_the_file_name_is_readable_across_origins(self):
        """The browser may only read a response header CORS names: without
        Content-Disposition on that list the client falls back to a generic
        name for every export. The middleware is configured at import from
        the settings, so its configuration is what is checked."""
        from fastapi.middleware.cors import CORSMiddleware
        from app.main import app
        cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        exposed = {h.lower() for h in cors.kwargs["expose_headers"]}
        assert "content-disposition" in exposed
        assert "x-result-truncated" in exposed            # the ones that were there stay

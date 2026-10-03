"""The spreadsheet export end to end against a real ArcadeDB: seeded owners,
subsidiaries over two levels, a role, a claim and a source come back as typed
cells on the right sheets, through the route."""
import io
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app.claims import KIND_OWNS, record_claim

pytestmark = pytest.mark.integration

NS = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
      "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0"}


def sheets_of(data: bytes) -> dict[str, list[list[tuple[str | None, str]]]]:
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("content.xml"))
    out = {}
    for tbl in root.iter(f"{{{NS['table']}}}table"):
        out[tbl.get(f"{{{NS['table']}}}name")] = [
            [(c.get(f"{{{NS['office']}}}value-type"), "".join(c.itertext()))
             for c in row.iter(f"{{{NS['table']}}}table-cell")]
            for row in tbl.iter(f"{{{NS['table']}}}table-row")]
    return out


def column(rows, name):
    idx = [c[1] for c in rows[0]].index(name)
    return [r[idx][1] for r in rows[1:]]


def _seed(it_db):
    it_db.run_command("CREATE (:Source {id: 'sec', name: 'SEC EDGAR', type: 'register', credibility_score: 98})")
    for eid, name in (("ms", "Microsoft"), ("li", "LinkedIn"), ("lii", "LinkedIn Ireland"),
                      ("gh", "GitHub"), ("br", "BlackRock"), ("old", "Former Owner")):
        it_db.run_command("CREATE (:Entity {id: $id, name: $n, type: 'company', country: 'US'})", {"id": eid, "n": name})
    def owns(a, b, **props):
        sets = ", ".join(f"{k}: ${k}" for k in props)
        it_db.run_command(f"MATCH (a:Entity {{id: $a}}), (b:Entity {{id: $b}}) "
                          f"CREATE (a)-[:OWNS {{source_id: 'sec', {sets}}}]->(b)", {"a": a, "b": b, **props})
    owns("br", "ms", stake_percent=7.3, ownership_type="minority", since="2024-02-13",
         source_url="https://example.com/13g", source_date="2024-02-13")
    owns("old", "ms", stake_percent=12.0, ownership_type="minority", since="2010-01-01", until="2018-03-31")
    owns("ms", "li", stake_percent=100.0, ownership_type="full", since="2016-12-08")
    owns("li", "lii", stake_percent=100.0, ownership_type="full")
    owns("ms", "gh", ownership_type="unknown")                      # no stake stated
    it_db.run_command("CREATE (:Person {id: 'sn', full_name: 'Satya Nadella', nationality: 'IN'})")
    it_db.run_command("MATCH (p:Person {id: 'sn'}), (e:Entity {id: 'ms'}) "
                      "CREATE (p)-[:HAS_ROLE {role: 'CEO', since: '2014-02-04', source_id: 'sec'}]->(e)")
    record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec", stake_percent=7.3,
                 ownership_type="minority", credibility_score=98, source_url="https://example.com/13g")


def test_the_present_direct_export(it_db, client):
    _seed(it_db)
    r = client.get("/v1/export/entity/ms?link=https://owlgraph.example/%23graph/e/ms")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.oasis.opendocument.spreadsheet")
    assert r.headers["content-disposition"].startswith('attachment; filename="Microsoft.ods"')
    s = sheets_of(r.content)
    assert list(s) == ["Overview", "Owners", "Subsidiaries", "Roles", "Timeline", "Sources", "Claims"]

    overview = {row[0][1]: row[1][1] for row in s["Overview"][1:] if row[0][1]}
    assert overview["Name"] == "Microsoft" and overview["Owners"] == "1" and overview["Subsidiaries (direct)"] == "2"
    assert overview["Live graph"] == "https://owlgraph.example/#graph/e/ms"

    owners = s["Owners"]
    assert column(owners, "Owner") == ["BlackRock"]                       # the former owner ended in 2018
    assert owners[1][[c[1] for c in owners[0]].index("Stake")] == ("percentage", "7.3 %")
    assert column(owners, "Source") == ["SEC EDGAR"]
    assert column(owners, "Source URL") == ["https://example.com/13g"]
    assert column(owners, "Also asserted by") == ["SEC EDGAR"]

    subs = s["Subsidiaries"]
    assert sorted(column(subs, "Company")) == ["GitHub", "LinkedIn"]       # direct only
    assert s["Roles"][1][:2] == [("string", "Satya Nadella"), ("string", "CEO")]
    assert sorted(column(s["Timeline"], "Party")) == ["BlackRock", "Former Owner", "GitHub", "LinkedIn", "Satya Nadella"]
    assert column(s["Sources"], "Source") == ["SEC EDGAR"]
    assert [(row[1][1], row[2][1]) for row in s["Claims"][1:]] == [("BlackRock", "Microsoft")]


def test_all_levels_as_of_and_the_stake_filter(it_db, client):
    _seed(it_db)
    r = client.get("/v1/export/entity/ms?all_levels=true&as_of=2015-12-31&min_stake=10")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].startswith('attachment; filename="Microsoft as of 2015.ods"')
    s = sheets_of(r.content)
    assert column(s["Owners"], "Owner") == ["Former Owner"]              # 7.3 % is under the filter; 2015: the old owner held
    subs = s["Subsidiaries"]
    assert [c[1] for c in subs[0]][:3] == ["Level", "Parent", "Company"]
    # LinkedIn began in 2016, so in 2015 only GitHub (unstated stake, kept) hangs under Microsoft
    assert [(row[0][1], row[1][1], row[2][1]) for row in subs[1:]] == [("1", "Microsoft", "GitHub")]
    overview = {row[0][1]: row[1][1] for row in s["Overview"][1:] if row[0][1]}
    assert overview["As of"] == "2015-12-31" and overview["Subsidiaries"] == "all levels"
    assert overview["Minimum stake"] == ">= 10 %"

    r = client.get("/v1/export/entity/ms?all_levels=true")
    rows = sheets_of(r.content)["Subsidiaries"][1:]
    assert [(row[0][1], row[1][1], row[2][1]) for row in rows] == [
        ("1", "Microsoft", "GitHub"), ("1", "Microsoft", "LinkedIn"), ("2", "LinkedIn", "LinkedIn Ireland")]


def test_an_unknown_company_is_404(it_db, client):
    assert client.get("/v1/export/entity/nope").status_code == 404

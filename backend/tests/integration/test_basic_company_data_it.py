"""
Real-ArcadeDB test for the Companies House BasicCompanyData importer: pre-creates
a number-keyed (un-named) company node the way the PSC import would, then imports a
tiny synthetic BasicCompanyData CSV and asserts the node is enriched (name, type,
incorporated, former-name aliases, searchable) — and that a register row for a company
NOT already in the graph is a no-op (enrichment only, no isolated nodes created).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import csv
import io
import zipfile

import pytest

pytestmark = pytest.mark.integration

_HEADER = ["CompanyName", "CompanyNumber", "RegAddress.AddressLine1",
           "RegAddress.PostTown", "RegAddress.PostCode", "CompanyCategory",
           "IncorporationDate", "PreviousName_1.CompanyName", "SICCode.SicText_1"]


def _basic_zip(tmp_path):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADER)
    w.writerow(["MONZO BANK LIMITED", "09446231", "BROADWALK HOUSE", "LONDON",
                "EC2A 2DA", "Private Limited Company", "24/02/2015", "MONDO LTD",
                "64191 - Banks"])
    w.writerow(["NOT IN GRAPH LTD", "99999999", "NOWHERE", "LONDON", "N1 1AA",
                "Private Limited Company", "01/01/2000", "", "70100 - Activities of head offices"])
    w.writerow(["FEVERTREE DRINKS PLC", "05334698", "107 CHEAPSIDE", "LONDON", "EC2V 6DN",
                "Public Limited Company", "18/01/2005", "", "70100 - Activities of head offices"])
    zpath = tmp_path / "basic.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("BasicCompanyDataAsOneFile-2026-10-01.csv", buf.getvalue())
    return str(zpath)


def test_enriches_existing_company_only(it_db, tmp_path):
    from app.scraper.basic_company_data import import_basic_company_data

    # PSC would have created this controlled company number-keyed and un-named,
    # with a PSC notified in the register's first year — a lower bound once the
    # incorporation date (2015) says the company predates the register.
    it_db.run_sql(
        "UPDATE Entity SET companies_house_id = '09446231', source_id = 'ukpsc' "
        "UPSERT WHERE id = 'gb-coh:09446231'")
    it_db.run_command("CREATE (:Entity {id:'owner'})")
    it_db.run_command("MATCH (a:Entity {id:'owner'}), (c:Entity {id:'gb-coh:09446231'}) "
                      "CREATE (a)-[:OWNS {since:'2016-07-04', filing_type:'PSC', until:null}]->(c)")

    result = import_basic_company_data(_basic_zip(tmp_path), 97)
    assert result["rows"] == 3 and result["companies"] == 3 and result["errors"] == 0
    assert result["first_year_bounds"]["edges"] == 1
    bound = it_db.run_command("MATCH ()-[r:OWNS]->(c:Entity {id:'gb-coh:09446231'}) "
                              "RETURN r.since_basis AS b")
    assert [r["b"] for r in bound] == ["register_first_year"]

    rec = it_db.run_command(
        "MATCH (e:Entity {id:'gb-coh:09446231'}) "
        "RETURN e.name AS name, e.type AS type, e.founded AS founded, "
        "e.incorporated_on AS incorporated_on, "
        "e.aliases AS aliases, e.search_text AS st, e.registered_address AS addr, "
        "e.reg_street AS rs, e.reg_city AS rc, e.reg_postcode AS rp, e.address AS a, "
        "e.hq_address AS ha, e.hq_city AS hc, e.hq_country AS hk, e.hq_street AS hs, e.hq_postcode AS hp")
    assert rec, "existing company should be enriched"
    row = rec[0]
    # the registered office in parts under the registered pin (dropped until 2026-10-18)
    assert (row["rs"], row["rc"], row["rp"]) == ("BROADWALK HOUSE", "LONDON", "EC2A 2DA")
    # the display address is the same string as the dedup corroborator
    assert row["a"] == row["addr"]
    # and nothing under hq_*: the register does not say where the company is run
    assert (row["ha"], row["hc"], row["hk"], row["hs"], row["hp"]) == (None,) * 5
    assert row["name"] == "MONZO BANK LIMITED"
    assert row["type"] == "company"
    assert row["incorporated_on"] == "2015-02-24"   # the legal entity's start, in Details
    assert row["founded"] is None                    # the business's origin is not the register's to say
    assert "MONDO LTD" in (row["aliases"] or [])
    assert "MONDO LTD" in (row["st"] or "")          # former name is searchable
    assert "EC2A 2DA" in (row["addr"] or "")

    # the register row for a company not already in the graph must NOT create a node
    missing = it_db.run_command("MATCH (e:Entity {id:'gb-coh:99999999'}) RETURN e.id AS id")
    assert not missing, "enrichment must not create isolated company nodes"


def test_a_company_held_under_a_gleif_id_is_enriched_too(it_db, tmp_path):
    """Fever-Tree lives under its LEI: GLEIF keyed the node `lei:…` with the
    Companies House number in companies_house_id, and no `gb-coh:` twin exists (a
    listed company has no PSC). Until 2026-10-24 the register row for it was a
    silent no-op — 207 of 238 UK companies on dev had no register name, address
    or SIC codes. The row now reaches the survivor, and so do the passes that
    follow the flush: its codes become statements with the register link."""
    from app.industry import TYPE
    from app.scraper.basic_company_data import import_basic_company_data

    it_db.run_command(
        "CREATE (:Entity {id:'lei:2138007AJRLHRVQZLD07', name:'FEVERTREE DRINKS PLC', "
        "lei_id:'2138007AJRLHRVQZLD07', companies_house_id:'05334698', country:'GB'})")

    result = import_basic_company_data(_basic_zip(tmp_path), 97)
    assert result["survivors"] == 1
    assert result["industry"] == 1          # Fever-Tree's one code; the other two rows reach no node

    row = it_db.run_command(
        "MATCH (e:Entity {id:'lei:2138007AJRLHRVQZLD07'}) "
        "RETURN e.incorporated_on AS fd, e.registered_address AS addr, e.uk_sic_codes AS sic, "
        "e.name_credibility AS nc, e.nace_code AS nace")[0]
    assert row["fd"] == "2005-01-18"
    assert "EC2V 6DN" in row["addr"]
    assert row["sic"] == ["70100 - Activities of head offices"]
    assert row["nc"] == 97
    assert row["nace"] == "70.10"
    # no number-keyed twin was created on the way
    assert not it_db.run_command("MATCH (e:Entity {id:'gb-coh:05334698'}) RETURN e.id AS id")
    # the statement is the survivor's, with the register page as its link
    stmt = it_db.run_sql(f"SELECT code, source_url FROM {TYPE} WHERE entity_id = 'lei:2138007AJRLHRVQZLD07'")
    assert [(r["code"], r["source_url"]) for r in stmt] == [
        ("70100", "https://find-and-update.company-information.service.gov.uk/company/05334698")]

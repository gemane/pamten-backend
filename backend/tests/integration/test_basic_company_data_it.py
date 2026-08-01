"""
Real-ArcadeDB test for the Companies House BasicCompanyData importer: pre-creates
a number-keyed (un-named) company node the way the PSC import would, then imports a
tiny synthetic BasicCompanyData CSV and asserts the node is enriched (name, type,
founded, former-name aliases, searchable) — and that a register row for a company
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
           "IncorporationDate", "PreviousName_1.CompanyName"]


def _basic_zip(tmp_path):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADER)
    w.writerow(["MONZO BANK LIMITED", "09446231", "BROADWALK HOUSE", "LONDON",
                "EC2A 2DA", "Private Limited Company", "24/02/2015", "MONDO LTD"])
    w.writerow(["NOT IN GRAPH LTD", "99999999", "NOWHERE", "LONDON", "N1 1AA",
                "Private Limited Company", "01/01/2000", ""])
    zpath = tmp_path / "basic.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("BasicCompanyData.csv", buf.getvalue())
    return str(zpath)


def test_enriches_existing_company_only(it_db, tmp_path):
    from app.scraper.basic_company_data import import_basic_company_data

    # PSC would have created this controlled company number-keyed and un-named.
    it_db.run_sql(
        "UPDATE Entity SET companies_house_id = '09446231', source_id = 'ukpsc' "
        "UPSERT WHERE id = 'gb-coh:09446231'")

    result = import_basic_company_data(_basic_zip(tmp_path), 97)
    assert result["rows"] == 2 and result["companies"] == 2 and result["errors"] == 0

    rec = it_db.run_command(
        "MATCH (e:Entity {id:'gb-coh:09446231'}) "
        "RETURN e.name AS name, e.type AS type, e.founded AS founded, "
        "e.founded_date AS founded_date, "
        "e.aliases AS aliases, e.search_text AS st, e.registered_address AS addr")
    assert rec, "existing company should be enriched"
    row = rec[0]
    assert row["name"] == "MONZO BANK LIMITED"
    assert row["type"] == "company"
    assert row["founded"] == 2015                 # headline = year
    assert row["founded_date"] == "2015-02-24"    # full date in Details
    assert "MONDO LTD" in (row["aliases"] or [])
    assert "MONDO LTD" in (row["st"] or "")          # former name is searchable
    assert "EC2A 2DA" in (row["addr"] or "")

    # the register row for a company not already in the graph must NOT create a node
    missing = it_db.run_command("MATCH (e:Entity {id:'gb-coh:99999999'}) RETURN e.id AS id")
    assert not missing, "enrichment must not create isolated company nodes"

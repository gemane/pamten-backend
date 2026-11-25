"""Insiders (app/scraper/sec_insiders.py): Form 3/4/5 parsing, role titles and dates, the insider
window, an insider's holding, departures from 8-K Item 5.02.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""
import textwrap
from unittest.mock import patch


from app.scraper.sec_insiders import _title_to_role, _parse_form34_xml


class TestTitleToRole:
    """_title_to_role maps officer titles to canonical role strings."""

    def test_ceo(self):
        assert _title_to_role("Chief Executive Officer") == "CEO"

    def test_ceo_abbrev(self):
        assert _title_to_role("CEO") == "CEO"

    def test_cfo(self):
        assert _title_to_role("Chief Financial Officer") == "CFO"

    def test_cto(self):
        assert _title_to_role("Chief Technology Officer") == "CTO"

    def test_general_counsel(self):
        assert _title_to_role("General Counsel") == "General Counsel"

    def test_chairman(self):
        assert _title_to_role("Executive Chairman") == "Chairman"

    def test_president(self):
        assert _title_to_role("President") == "President"

    def test_vp_is_not_president(self):
        # "vice president" contains "president" but must NOT match
        role = _title_to_role("Vice President of Engineering")
        assert role != "President"

    def test_unknown_passthrough(self):
        # Non-standard titles are returned as-is
        assert _title_to_role("SVP Powertrain and Energy Eng.") == "SVP Powertrain and Energy Eng."

    def test_empty(self):
        assert _title_to_role("") == "Officer"


class TestParseForm34Xml:
    """_parse_form34_xml extracts name/role from Form 3/4 XML."""

    def _make_xml(self, name: str, is_officer: str = "0", is_director: str = "0",
                   title: str = "", remarks: str = "") -> str:
        return textwrap.dedent(f"""
            <?xml version="1.0"?>
            <ownershipDocument>
              <reportingOwner>
                <reportingOwnerId>
                  <rptOwnerName>{name}</rptOwnerName>
                </reportingOwnerId>
                <reportingOwnerRelationship>
                  <isOfficer>{is_officer}</isOfficer>
                  <isDirector>{is_director}</isDirector>
                  <officerTitle>{title}</officerTitle>
                </reportingOwnerRelationship>
              </reportingOwner>
              <remarks>{remarks}</remarks>
            </ownershipDocument>
        """).strip()

    def test_officer_with_title(self):
        xml = self._make_xml("MUSK ELON", is_officer="1", title="Chief Executive Officer")
        result = _parse_form34_xml(xml)
        assert result is not None
        assert result["name"] == "Elon Musk"
        assert result["role"] == "CEO"
        assert result["title"] == "Chief Executive Officer"

    # Berkshire's 2026 Form 4s (accessions 0001193125-26-349555 / -349574):
    # the title box says "See Remarks", the remarks box holds the title.
    def test_see_remarks_reads_the_role_off_the_remarks_box(self):
        xml = self._make_xml("Chang Charles C", is_officer="1", title="See Remarks",
                             remarks="Senior Vice President and Chief Financial Officer")
        result = _parse_form34_xml(xml)
        assert result["role"] == "CFO"
        assert result["title"] == "Senior Vice President and Chief Financial Officer"
        xml = self._make_xml("O'Sullivan Michael J.", is_officer="1", title="See remarks below",
                             remarks="Senior Vice President, General Counsel and Secretary")
        assert _parse_form34_xml(xml)["role"] == "General Counsel"

    # Televisa's Form 4 (CIK 912892, 2026-06-08): the remarks are a sentence
    # about a post at a SUBSIDIARY — mining it for "Chief Executive Officer"
    # would have seated him as Televisa's CEO.
    def test_a_remarks_sentence_is_never_mined_for_a_role(self):
        from app.scraper.sec_insiders import _looks_like_title, _officer_role
        valim = ("Mr. Valim is the Chief Executive Officer of izzi, Grupo Televisa, S.A.B.'s cable "
                 "company, and on that basis he may be considered an officer of Grupo Televisa, S.A.B.")
        xml = self._make_xml("Valim Francisco", is_officer="1", title="See Remarks", remarks=valim)
        result = _parse_form34_xml(xml)
        assert result["role"] == "Officer"
        assert result["title"] == "See Remarks"                 # the sentence is not a title either
        assert _officer_role("See Remarks", "He serves as Chief Financial Officer of the parent.") == "Officer"
        assert _officer_role("See Remarks", "Chief Financial Officer. Also a director of the trust.") == "Officer"
        assert _officer_role("See Remarks", "Chief Financial Officer " * 12) == "Officer"     # too long for a title
        # what a title looks like
        for ok in ("Senior Vice President and Chief Financial Officer", "Vice Chairman - Non-Insurance Operations",
                   "Senior Vice President, General Counsel and Secretary"):
            assert _looks_like_title(ok), ok
        assert _officer_role("See Remarks", "Senior Vice President and Chief Financial Officer") == "CFO"

    def test_see_remarks_never_becomes_a_role_and_a_remarks_paragraph_never_a_title(self):
        from app.scraper.sec_insiders import _officer_role
        # remarks that name no role we know: a plain officer, not the paragraph
        xml = self._make_xml("DOE JANE", is_officer="1", title="See Remarks",
                             remarks="Senior Vice President - Insurance Operations. The reporting "
                                     "person disclaims beneficial ownership of the shares held by the trust.")
        result = _parse_form34_xml(xml)
        assert result["role"] == "Officer"
        # no remarks at all: still never the placeholder
        assert _parse_form34_xml(self._make_xml("DOE JANE", is_officer="1", title="See Remarks"))["role"] == "Officer"
        assert _officer_role("See Remarks", "") == "Officer"
        assert _officer_role("see remarks", "Vice Chairman - Non-Insurance Operations") == "Chairman"
        # an ordinary title box is read as before, remarks or not
        assert _officer_role("Chief Executive Officer", "unrelated remarks") == "CEO"
        assert _officer_role("Treasurer", "Chief Financial Officer") == "Treasurer"

    def test_director(self):
        xml = self._make_xml("KIMBAL MUSK", is_director="1")
        result = _parse_form34_xml(xml)
        assert result is not None
        assert result["role"] == "Director"

    def test_neither_officer_nor_director_returns_none(self):
        # Pure investor (Form 4, non-affiliate) — should be skipped
        xml = self._make_xml("SOME FUND", is_officer="0", is_director="0")
        assert _parse_form34_xml(xml) is None

    def test_missing_reporting_owner_returns_none(self):
        xml = "<ownershipDocument><issuer/></ownershipDocument>"
        assert _parse_form34_xml(xml) is None

    def test_invalid_xml_returns_none(self):
        assert _parse_form34_xml("this is not xml") is None

    def test_xslt_rendered_html_returns_none(self):
        # When the XSLT-prefixed URL is fetched instead of raw XML, we get HTML
        html = "<html><body><p>Filing viewer</p></body></html>"
        assert _parse_form34_xml(html) is None


class TestXsltPrefixStripping:
    """
    Regression test for the XSLT-prefix bug.

    EDGAR's primaryDocument field sometimes contains a stylesheet prefix:
      'xslF345X06/tm2618092-2_4seq1.xml'
    Fetching that path returns an HTML-rendered view, not raw XML.
    The fix: take only the last path component.
    """

    def test_prefix_stripped(self):
        raw = "xslF345X06/tm2618092-2_4seq1.xml"
        fixed = raw.split("/")[-1] if "/" in raw else raw
        assert fixed == "tm2618092-2_4seq1.xml"

    def test_no_prefix_unchanged(self):
        raw = "form4.xml"
        fixed = raw.split("/")[-1] if "/" in raw else raw
        assert fixed == "form4.xml"


# ── Form 4 share-holding extraction ──────────────────────────────────────────

_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Fink Laurence D</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>500000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeHolding>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>510000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeHolding>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4_extracts_role_and_shares_owned():
    out = _parse_form34_xml(_FORM4_XML)
    assert out["role"] == "CEO"
    assert out["shares_owned"] == 510000.0   # the LAST row: the position the filing leaves the filer in


def test_parse_form4_shares_none_when_absent():
    xml = """<ownershipDocument><reportingOwner>
      <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
    </reportingOwner></ownershipDocument>"""
    out = _parse_form34_xml(xml)
    assert out["role"] == "Director" and out["shares_owned"] is None


# ── The holding, per class, against the count of its own class ──────────────

# Warren Buffett's Form 4 of 15 July 2026: 8,000 Class A converted into
# 12,000,000 Class B, then given away in four gifts; the Class A he keeps is in
# the derivative table, because Class A is convertible into B. The largest
# "owned following" value (12,001,162) is the moment before the gifts.
_BUFFETT_XML = """<ownershipDocument>
  <issuer><issuerCik>0001067983</issuerCik></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>BUFFETT WARREN E</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
      <officerTitle>Chairman and CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction><securityTitle><value>Class B Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>12001162</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
    <nonDerivativeTransaction><securityTitle><value>Class B Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>3001162</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
    <nonDerivativeTransaction><securityTitle><value>Class B Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>1162</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction><securityTitle><value>Class A Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>188290</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></derivativeTransaction>
    <derivativeHolding><securityTitle><value>Stock Option (Right to Buy)</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>999999</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </derivativeHolding>
  </derivativeTable>
</ownershipDocument>"""


# Berkshire's cover: 488,450 Class A and 1,408,035,161 Class B as of 2026-07-29.
BRK = {"total": 1_408_523_611.0, "as_of": "2026-07-29", "by_class": {"A": 488_450.0, "B": 1_408_035_161.0}}


class TestForm4Holdings:
    def test_the_last_row_per_security_not_the_largest(self):
        from app.scraper.sec_insiders import _form4_holdings
        import xml.etree.ElementTree as ET
        h = _form4_holdings(ET.fromstring(_BUFFETT_XML))
        assert h["Class B Common Stock"] == 1162.0          # after the gifts, not 12,001,162 before them
        assert h["Class A Common Stock"] == 188290.0        # a plain class of common stock in Table II counts
        assert "Stock Option (Right to Buy)" not in h       # an option is not a share held

    def test_direct_and_indirect_positions_of_one_security_are_summed(self):
        from app.scraper.sec_insiders import _form4_holdings
        import xml.etree.ElementTree as ET
        xml = """<x><nonDerivativeTable>
          <nonDerivativeHolding><securityTitle><value>Common Stock</value></securityTitle>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>100</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeHolding>
          <nonDerivativeHolding><securityTitle><value>Common Stock</value></securityTitle>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>40</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
              <natureOfOwnership><value>By trust</value></natureOfOwnership></ownershipNature></nonDerivativeHolding>
        </nonDerivativeTable></x>"""
        assert _form4_holdings(ET.fromstring(xml)) == {"Common Stock": 140.0}

    def test_the_stake_is_class_against_class(self):
        from app.scraper.sec_insiders import stake_for
        pick = stake_for({"Class B Common Stock": 1162.0, "Class A Common Stock": 188290.0}, BRK)
        # the class with the largest percentage carries the edge
        assert pick["share_class"] == "Class A Common Stock" and pick["shares_owned"] == 188290.0
        assert pick["stake_percent"] == 38.5485                  # of the 488,450 Class A, never of a B count
        assert pick["shares_outstanding"] == 488_450.0 and pick["denominator_date"] == "2026-07-29"

    def test_a_count_of_one_class_is_never_divided_by_another(self):
        from app.scraper.sec_insiders import stake_for
        # only Class B held: 1,162 of 1.4 bn Class B — a rounding-floor 0.0001 %, and never 1,274 %
        pick = stake_for({"Class B Common Stock": 1162.0}, BRK)
        assert pick["stake_percent"] == 0.0001 and pick["shares_outstanding"] == 1_408_035_161.0
        # a class the issuer does not state, or no class named at all, on a multi-class issuer: no denominator
        for title in ("Class C Common Stock", "Common Stock"):
            pick = stake_for({title: 5000.0}, BRK)
            assert pick["stake_percent"] is None and pick["shares_outstanding"] is None
            assert pick["denominator_date"] is None

    def test_an_unclassed_issuer_total_divides_any_title(self):
        from app.scraper.sec_insiders import stake_for
        assert stake_for({"Common Stock": 2000.0}, 100_000)["stake_percent"] == 2.0          # a bare total, as the 13F path hands over
        assert stake_for({"Class A Common Stock": 2000.0}, {"total": 100_000, "as_of": "2026-05-01", "by_class": {}})["stake_percent"] == 2.0

    def test_nothing_held_is_no_pick(self):
        from app.scraper.sec_insiders import stake_for
        assert stake_for({}, BRK) is None
        assert stake_for({"Common Stock": 0.0}, BRK) is None

    def test_the_parsed_filing_carries_its_class(self):
        out = _parse_form34_xml(_BUFFETT_XML)
        assert out["shares_owned"] == 188290.0 and out["share_class"] == "Class A Common Stock"
        assert out["holdings"] == {"Class B Common Stock": 1162.0, "Class A Common Stock": 188290.0}


# ── Person-centric insider holding (Option A) ────────────────────────────────

_SUBS = {"filings": {"recent": {
    "form": ["4"], "accessionNumber": ["0001059245-24-000123"],
    "primaryDocument": ["form4.xml"], "filingDate": ["2024-02-01"]}}}


def _form4(issuer_cik: str, shares: str) -> str:
    return (f"<x><issuerCik>{issuer_cik}</issuerCik>"
            f"<nonDerivativeTable><nonDerivativeTransaction><postTransactionAmounts>"
            f"<sharesOwnedFollowingTransaction><value>{shares}</value>"
            f"</sharesOwnedFollowingTransaction></postTransactionAmounts>"
            f"</nonDerivativeTransaction></nonDerivativeTable></x>")


def test_fetch_insider_holding_computes_stake_when_issuer_matches():
    from app.scraper.sec_insiders import fetch_insider_holding
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_client._get", return_value=_SUBS), \
         patch("app.scraper.sec_client._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", shares_outstanding=100000)
    assert out["shares_owned"] == 2000.0
    assert out["stake_percent"] == 2.0            # 2000 / 100000 * 100


def test_fetch_insider_holding_divides_class_by_class():
    from app.scraper.sec_insiders import fetch_insider_holding
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_client._get", return_value=_SUBS), \
         patch("app.scraper.sec_client._get_text", return_value=_BUFFETT_XML), \
         patch("time.sleep"):
        out = fetch_insider_holding("Warren Buffett", "0001067983", shares_outstanding=BRK)
    assert out["shares_owned"] == 188290.0 and out["share_class"] == "Class A Common Stock"
    assert out["stake_percent"] == 38.5485 and out["denominator_date"] == "2026-07-29"
    assert out["source_date"] == "2024-02-01"


def test_fetch_insider_holding_none_when_issuer_mismatch():
    from app.scraper.sec_insiders import fetch_insider_holding
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_client._get", return_value=_SUBS), \
         patch("app.scraper.sec_client._get_text", return_value=_form4("0000999999", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", shares_outstanding=100000)
    assert out is None                            # Form 4 is about a different company


def test_fetch_insider_holding_none_when_no_cik():
    from app.scraper.sec_insiders import fetch_insider_holding
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=[]):
        assert fetch_insider_holding("Nobody", "0001364742") is None


def test_fetch_insider_holding_returns_the_cik_it_was_filed_under():
    from app.scraper.sec_insiders import fetch_insider_holding
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_client._get", return_value=_SUBS), \
         patch("app.scraper.sec_client._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742")
    assert out["cik"] == "0001059245"


def test_fetch_insider_holding_tries_the_next_candidate():
    """A name search with several matches names no one: the first candidate
    files about another company, the second about the issuer."""
    from app.scraper.sec_insiders import fetch_insider_holding
    xml = {"1": _form4("0000999999", "50"), "2": _form4("0001364742", "2000")}
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=["0000000001", "0000000002"]), \
         patch("app.scraper.sec_client._get", return_value=_SUBS), \
         patch("app.scraper.sec_client._get_text", side_effect=lambda url: xml[url.split("/")[-3]]), \
         patch("time.sleep"):
        out = fetch_insider_holding("Thomas Murphy", "0001364742")
    assert out["cik"] == "0000000002" and out["shares_owned"] == 2000.0


def test_fetch_insider_holding_uses_the_cik_the_person_holds():
    from app.scraper.sec_insiders import fetch_insider_holding
    with patch("app.scraper.sec_lookup._lookup_person_ciks") as lookup, \
         patch("app.scraper.sec_client._get", return_value=_SUBS), \
         patch("app.scraper.sec_client._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", person_cik="1059245")
    lookup.assert_not_called()
    assert out["cik"] == "0001059245"


def test_fetch_insider_holding_opens_a_bounded_number_of_a_candidates_form_4s():
    from app.scraper import sec_insiders
    n = sec_insiders.MAX_PERSON_FORM4 + 5
    subs = {"filings": {"recent": {"form": ["4"] * n,
                                   "accessionNumber": [f"0001059245-24-{i:06d}" for i in range(n)],
                                   "primaryDocument": ["form4.xml"] * n,
                                   "filingDate": ["2024-02-01"] * n}}}
    with patch("app.scraper.sec_lookup._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_client._get", return_value=subs), \
         patch("app.scraper.sec_client._get_text", return_value=_form4("0000999999", "1")) as get_text, \
         patch("time.sleep"):
        assert sec_insiders.fetch_insider_holding("Larry Fink", "0001364742") is None
    assert get_text.call_count == sec_insiders.MAX_PERSON_FORM4


class TestForm34IssuerVerification:
    """The wrong-subject bug's second door: a company's submissions feed lists
    Form 3/4s it FILED about other issuers, and parsing them put "EMBRAER S.A."
    into Embraer's executive list as a Director — isDirector meant director of
    Eve Holding. The XML names the issuer's CIK exactly, so unlike the 13D
    cover there is nothing fuzzy about the check."""

    def _xml(self, owner: str, issuer_cik: str | None) -> str:
        issuer = f"<issuer><issuerCik>{issuer_cik}</issuerCik></issuer>" if issuer_cik else ""
        return (f'<?xml version="1.0"?><ownershipDocument>{issuer}'
                f'<reportingOwner><reportingOwnerId>'
                f'<rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>'
                f'<reportingOwnerRelationship><isDirector>1</isDirector>'
                f'</reportingOwnerRelationship></reportingOwner>'
                f'</ownershipDocument>')

    def test_the_parser_reports_the_issuer(self):
        out = _parse_form34_xml(self._xml("EMBRAER S.A.", "0001823652"))
        assert out["issuer_cik"] == "0001823652"

    def test_a_form_about_another_issuer_is_dropped(self):
        # Embraer (CIK 1355444) scanning its feed must not keep a Form 4 whose
        # issuer is Eve Holding (1823652), whoever the reporting owner is.
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders

        submissions = {"filings": {"recent": {
            "form":            ["4", "4"],
            "accessionNumber": ["0001292814-24-000001", "0000000001-24-000002"],
            "primaryDocument": ["eve.xml", "own.xml"],
            "filingDate":      ["2024-09-05", "2024-09-06"],
        }}}
        docs = {
            "eve.xml": self._xml("EMBRAER S.A.", "0001823652"),      # about Eve
            "own.xml": self._xml("GOMES NETO FRANCISCO", "1355444"),  # about Embraer
        }

        def fake_get(url, params=None):
            return submissions

        def fake_get_text(url, params=None):
            return docs[url.rsplit("/", 1)[-1]]

        with patch.object(sec_client, "_get", side_effect=fake_get), \
             patch.object(sec_client, "_get_text", side_effect=fake_get_text):
            execs = sec_insiders.fetch_executives("1355444")

        names = [e["name"] for e in execs]
        assert "Embraer S.A." not in str(names) and "EMBRAER" not in str(names), \
            "the Eve Holding Form 4 leaked the company into its own executive list"
        assert len(execs) == 1 and "Francisco" in execs[0]["name"]

    def test_a_form_with_no_issuer_cik_is_kept(self):
        # Absent metadata is not a mismatch — same principle as the 13D check.
        out = _parse_form34_xml(self._xml("SOME PERSON", None))
        assert out is not None and out["issuer_cik"] is None

    def test_padding_does_not_defeat_the_comparison(self):
        # issuerCik may or may not be zero-padded; 1355444 == 0001355444.
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders

        submissions = {"filings": {"recent": {
            "form": ["4"], "accessionNumber": ["0000000001-24-000003"],
            "primaryDocument": ["p.xml"], "filingDate": ["2024-01-01"],
        }}}
        with patch.object(sec_client, "_get", return_value=submissions), \
             patch.object(sec_client, "_get_text",
                          return_value=self._xml("GOMES NETO FRANCISCO", "0001355444")):
            execs = sec_insiders.fetch_executives("1355444")
        assert len(execs) == 1


class TestRoleDatesFromForm3:
    """Form 4 dates a trade, not a seat; Form 3 is filed within ten days of
    becoming an insider and its periodOfReport IS the appointment date (John
    Ternus: 2026-09-01, Apple's stated transition date). Until now the role
    edges from SEC carried no start at all — 204 of them on dev, 0 dated by
    SEC itself.
    """

    def _xml(self, name, *, form="4", period="2026-04-27", officer="1", director="0",
             title="", other="", cik="0001234"):
        other_xml = (f"<isOther>1</isOther><otherText>{other}</otherText>" if other else "")
        return (f'<?xml version="1.0"?><ownershipDocument><documentType>{form}</documentType>'
                f'<periodOfReport>{period}</periodOfReport>'
                f'<issuer><issuerCik>0000320193</issuerCik></issuer>'
                f'<reportingOwner><reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik>'
                f'<rptOwnerName>{name}</rptOwnerName></reportingOwnerId>'
                f'<reportingOwnerRelationship><isDirector>{director}</isDirector>'
                f'<isOfficer>{officer}</isOfficer><officerTitle>{title}</officerTitle>'
                f'{other_xml}</reportingOwnerRelationship></reportingOwner></ownershipDocument>')

    def test_flags_written_as_true_are_read(self):
        # Apple's 2026 Form 3 for its new CEO: <isDirector>true</isDirector>.
        out = _parse_form34_xml(self._xml("Ternus John", form="3", period="2026-09-01",
                                          officer="true", director="true", title="CEO"))
        assert out and out["role"] == "CEO" and out["document_type"] == "3"
        assert out["period_of_report"] == "2026-09-01" and out["former"] is False

    def test_a_former_officer_filing_names_the_seat_that_ended(self):
        out = _parse_form34_xml(self._xml("Doe Jane", officer="0", director="0",
                                          other="Former Chief Financial Officer",
                                          period="2026-03-31"))
        assert out and out["former"] is True and out["role"] == "CFO"
        assert out["period_of_report"] == "2026-03-31"

    def test_a_company_keeps_its_name_as_filed(self):
        # Embraer files Form 4s as a director of Eve Holding. Turned around as
        # a person's 'LAST FIRST' it became "S.a Embraer", lost its legal form
        # and was minted a Person with Embraer's CIK (findings item 21).
        out = _parse_form34_xml(self._xml("EMBRAER S.A.", officer="0", director="1",
                                          cik="0001355444"))
        assert out and out["name"] == "EMBRAER S.A." and out["person_cik"] == "0001355444"
        assert _parse_form34_xml(self._xml("COOK TIMOTHY D"))["name"] == "Timothy D Cook"

    def test_a_pure_investor_is_still_nobody(self):
        assert _parse_form34_xml(self._xml("Some Fund", officer="0", director="0",
                                           other="10% owner")) is None

    def _run(self, forms, docs, dates=None):
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders
        n = len(forms)
        submissions = {"filings": {"recent": {
            "form": forms,
            "accessionNumber": [f"0000000001-26-{i:06d}" for i in range(n)],
            "primaryDocument": [f"d{i}.xml" for i in range(n)],
            "filingDate": dates or [f"2026-0{9 - i}-01" for i in range(n)],
        }}}
        served = {f"d{i}.xml": d for i, d in enumerate(docs)}

        def fake_get_text(url, params=None):
            return served[url.split("/")[-1]]
        with patch.object(sec_client, "_get", return_value=submissions), \
             patch.object(sec_client, "_get_text", side_effect=fake_get_text):
            return sec_insiders.fetch_executives("320193")

    def test_the_form_3_behind_a_form_4_dates_the_seat(self):
        execs = self._run(
            ["4", "3"],
            [self._xml("Ternus John", form="4", period="2026-09-10", title="CEO"),
             self._xml("Ternus John", form="3", period="2026-09-01", title="CEO")])
        assert len(execs) == 1
        assert execs[0]["role"] == "CEO" and execs[0]["since"] == "2026-09-01"
        assert execs[0]["until"] is None

    def test_a_form_3_for_a_different_seat_does_not_date_the_current_one(self):
        # VP then, CEO now: the Form 3 dates the VP seat, which is not listed.
        execs = self._run(
            ["4", "3"],
            [self._xml("Ternus John", form="4", period="2026-09-10", title="CEO"),
             self._xml("Ternus John", form="3", period="2021-02-01", title="Vice President")])
        assert execs[0]["role"] == "CEO" and execs[0]["since"] is None

    def test_a_form_3_as_the_newest_filing_dates_itself(self):
        execs = self._run(["3"], [self._xml("Newstead Jennifer", form="3", period="2026-03-01",
                                            title="SVP, GC and Secretary")])
        assert execs[0]["since"] == "2026-03-01"

    def test_a_former_filing_becomes_a_closing_not_a_seat(self):
        execs = self._run(["4"], [self._xml("Doe Jane", officer="0", director="0",
                                            other="Former Chief Financial Officer",
                                            period="2026-03-31")])
        assert execs[0]["former"] is True and execs[0]["until"] == "2026-03-31"
        assert execs[0]["role"] == "CFO" and execs[0]["since"] is None

    def test_a_form_3_date_shared_by_the_whole_board_is_the_issuers_event(self):
        # Embraer became subject to Section 16 on 2026-03-18 and every sitting
        # officer and director filed a Form 3 that day: not their seat dates.
        execs = self._run(
            ["3", "3", "3", "3"],
            [self._xml(f"Person{i} A", form="3", period="2026-03-18", title="Director",
                       officer="0", director="1", cik=f"{i:07d}") for i in range(3)]
            + [self._xml("Lima Felipe", form="3", period="2026-04-13", title="CFO", cik="0000099")])
        assert [e["since"] for e in execs] == [None, None, None, "2026-04-13"], \
            "the shared date is dropped, the individual one kept"

    def _run_paged(self, recent_forms, recent_docs, pages, dated=None):
        """Like _run, with older `filings.files` pages: {name: (forms, docs)}."""
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders
        served, fetched_pages = {}, []

        def index(prefix, forms, docs):
            n = len(forms)
            for i, d in enumerate(docs):
                served[f"{prefix}{i}.xml"] = d
            return {"form": forms,
                    "accessionNumber": [f"0000000001-{prefix}-{i:06d}" for i in range(n)],
                    "primaryDocument": [f"{prefix}{i}.xml" for i in range(n)],
                    "filingDate": [f"20{prefix}-01-01"] * n}
        recent = index("26", recent_forms, recent_docs)
        # an older filing ends the inline page before the insider window starts,
        # so the older pages are read for seat dates only (the window: item 105)
        for key, value in (("form", "8-K"), ("accessionNumber", "0000000001-19-000000"),
                           ("primaryDocument", "8k.htm"), ("filingDate", "2019-01-01")):
            recent[key] = [*recent[key], value]
        submissions = {"filings": {"recent": recent,
                                   "files": [{"name": name} for name in pages]}}
        older = {name: index(name[:2], *fd) for name, fd in pages.items()}

        def fake_get(url, params=None):
            name = url.split("/")[-1]
            if name.startswith("CIK"):
                return submissions
            fetched_pages.append(name)
            return older[name]

        def fake_get_text(url, params=None):
            return served[url.split("/")[-1]]
        with patch.object(sec_client, "_get", side_effect=fake_get), \
             patch.object(sec_client, "_get_text", side_effect=fake_get_text):
            return sec_insiders.fetch_executives("320193"), fetched_pages

    def test_a_form_3_on_an_older_page_dates_a_long_serving_officer(self):
        # Amy Hood: Form 4s in the inline index since 2020, Form 3 (CFO,
        # 2013-05-08) on the 2008–2020 page.
        execs, pages = self._run_paged(
            ["4"], [self._xml("Hood Amy", form="4", title="CFO", cik="0000001")],
            {"13-page.json": (["3", "4"], [self._xml("Hood Amy", form="3", period="2013-05-08",
                                                     title="CFO", cik="0000001"),
                                           self._xml("Hood Amy", form="4", title="CFO", cik="0000001")])})
        assert execs[0]["since"] == "2013-05-08" and pages == ["13-page.json"]

    def test_a_former_insider_s_seat_is_dated_too(self):
        # findings item 74: the seat is written closed on a fresh read, so its
        # start is read like any other — Verra Mobility's CFO, "Former Chief
        # Financial Officer" on 2026-11-06, Form 3 as CFO on an older page
        execs, pages = self._run_paged(
            ["4", "4"], [self._xml("Conti Craig", form="4", officer="0", director="0",
                                   other="Former Chief Financial Officer", period="2026-11-05", cik="0000003"),
                         self._xml("Conti Craig", form="4", title="Chief Financial Officer", cik="0000003")],
            {"19-page.json": (["3"], [self._xml("Conti Craig", form="3", period="2019-04-01",
                                                title="Chief Financial Officer", cik="0000003")])})
        assert execs[0]["former"] is True and execs[0]["until"] == "2026-11-05"
        assert execs[0]["since"] == "2019-04-01" and pages == ["19-page.json"]

    def test_older_pages_are_not_read_when_everyone_is_dated(self):
        execs, pages = self._run_paged(
            ["3"], [self._xml("Hood Amy", form="3", period="2013-05-08", title="CFO")],
            {"13-page.json": (["3"], [self._xml("Hood Amy", form="3", period="2005-01-01", title="CFO")])})
        assert execs[0]["since"] == "2013-05-08" and pages == []

    def test_an_older_form_3_for_another_seat_leaves_the_current_one_undated(self):
        # Kathleen Hogan: Form 3 as EVP Human Resources (2014); current seat EVP Strategy.
        execs, pages = self._run_paged(
            ["4"], [self._xml("Hogan Kathleen", form="4", title="EVP, Strategy", cik="0000002")],
            {"14-page.json": (["3"], [self._xml("Hogan Kathleen", form="3", period="2014-11-28",
                                                title="EVP, Human Resources", cik="0000002")])})
        assert execs[0]["since"] is None and pages == ["14-page.json"]

    def test_reading_stops_once_the_last_person_is_dated(self):
        # Page one dates Hood; page two is never asked for.
        execs, fetched = self._run_paged(
            ["4"], [self._xml("Hood Amy", form="4", title="CFO", cik="0000001")],
            {"13-page.json": (["3"], [self._xml("Hood Amy", form="3", period="2013-05-08",
                                                title="CFO", cik="0000001")]),
             "05-page.json": (["3"], [self._xml("Hood Amy", form="3", period="2005-01-01",
                                                title="CFO", cik="0000001")])})
        assert execs[0]["since"] == "2013-05-08" and fetched == ["13-page.json"]

    def test_older_pages_stop_at_the_page_cap(self):
        from app.scraper import sec_insiders
        pages = {f"{10 + i}-page.json": (["3"], [self._xml("Nobody Else", form="3",
                                                             period="2010-01-01", cik="0000099")])
                 for i in range(sec_insiders.MAX_OLDER_PAGES + 2)}
        execs, fetched = self._run_paged(
            ["4"], [self._xml("Hood Amy", form="4", title="CFO", cik="0000001")], pages)
        assert execs[0]["since"] is None and len(fetched) == sec_insiders.MAX_OLDER_PAGES

    def test_form_3s_are_still_read_past_the_insider_cap(self):
        from app.scraper import sec_insiders
        cap = sec_insiders.MAX_FORM4_FETCH
        forms = ["4"] * cap + ["3"]
        docs = [self._xml(f"Person{i} A", title="Vice President", cik=f"{i:07d}") for i in range(cap)]
        docs.append(self._xml("Person0 A", form="3", period="2020-01-15", title="Vice President",
                              cik="0000000"))
        execs = self._run(forms, docs)
        assert len(execs) == cap
        assert execs[0]["since"] == "2020-01-15", "the cap stops new insiders, not their Form 3s"


class TestTheInsiderWindow:
    """Insiders come from the Form 3/4s of the last ~15 months, not from the
    newest 50 documents: a few officers' repeat filings used those up, and
    Bank of America listed 8 of its 29 insiders (findings item 9)."""

    _xml = TestRoleDatesFromForm3._xml
    _run = TestRoleDatesFromForm3._run

    def test_directors_behind_a_burst_of_officer_filings_are_listed(self):
        # 60 Form 4s by two officers, then the annual director grant
        forms = ["4"] * 64
        docs = [self._xml("Moynihan Brian", title="CEO", cik="0000001") if i % 2 else
                self._xml("Borthwick Alastair", title="CFO", cik="0000002") for i in range(60)]
        docs += [self._xml(f"Director{i} A", officer="0", director="1", cik=f"00001{i:02d}") for i in range(4)]
        dates = ["2026-10-29"] * 60 + ["2026-05-06"] * 4
        execs = self._run(forms, docs, dates)
        assert sorted(e["name"] for e in execs if e["role"] == "Director") == \
            [f"A Director{i}" for i in range(4)]

    def test_a_filing_before_the_window_lists_no_one(self):
        forms = ["4", "4"]
        docs = [self._xml("Moynihan Brian", title="CEO", cik="0000001"),
                self._xml("Budd Wayne", officer="0", director="1", cik="0000009")]
        execs = self._run(forms, docs, ["2026-10-29", "2025-07-01"])     # 485 days earlier
        assert [e["name"] for e in execs] == ["Brian Moynihan"]

    def test_a_form_3_before_the_window_still_dates_a_listed_seat(self):
        forms = ["4", "3"]
        docs = [self._xml("Moynihan Brian", title="CEO", cik="0000001"),
                self._xml("Moynihan Brian", form="3", period="2010-01-01", title="CEO", cik="0000001")]
        execs = self._run(forms, docs, ["2026-10-29", "2010-01-04"])
        assert execs[0]["since"] == "2010-01-01"

    def test_the_document_ceiling_still_bounds_the_scan(self):
        from unittest.mock import patch
        from app.scraper import sec_insiders
        with patch.object(sec_insiders, "MAX_FORM4_SCAN", 3):
            forms = ["4"] * 5
            docs = [self._xml(f"Person{i} A", title="Vice President", cik=f"{i:07d}") for i in range(5)]
            execs = self._run(forms, docs, ["2026-10-29"] * 5)
        assert len(execs) == 3

    def test_the_window_is_read_whole_past_the_old_ceiling(self):
        # TSMC, 2026-11-10: 333 Form 3/4s in eight months; its directors' one
        # filing, the Form 3 of 2026-03-18, came after the 300th (item 105)
        forms = ["4"] * 330 + ["3"] * 3
        docs = [self._xml("Wei C C", title="CEO", cik="0000001") for _ in range(330)]
        docs += [self._xml(f"Director{i} A", form="3", officer="0", director="1", period="2026-03-18",
                           cik=f"00001{i:02d}") for i in range(3)]
        execs = self._run(forms, docs, ["2026-11-10"] * 330 + ["2026-03-18"] * 3)
        assert sorted(e["name"] for e in execs if e["role"] == "Director") == \
            [f"A Director{i}" for i in range(3)]

    def test_the_window_reads_on_into_the_next_index_page(self):
        # the inline index ends inside the window (JPMorgan's on 2025-11-10,
        # the window from 2025-07-16): the director on the next page is listed,
        # a filing on that page before the window is not, and the page is read
        # once for the window and the seat dates together
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders
        recent = {"form": ["4"], "accessionNumber": ["0000000001-26-000000"],
                  "primaryDocument": ["r0.xml"], "filingDate": ["2026-10-29"]}
        older = {"form": ["4", "4", "3"],
                 "accessionNumber": ["0000000001-25-000001", "0000000001-24-000002", "0000000001-06-000003"],
                 "primaryDocument": ["o1.xml", "o2.xml", "o3.xml"],
                 "filingDate": ["2025-09-01", "2025-01-02", "2006-01-02"]}
        served = {"r0.xml": self._xml("Dimon James", title="CEO", cik="0000001"),
                  "o1.xml": self._xml("Crown James", officer="0", director="1", cik="0000002"),
                  "o2.xml": self._xml("Gone Long", officer="0", director="1", cik="0000003"),
                  "o3.xml": self._xml("Dimon James", form="3", period="2006-01-01", title="CEO",
                                      cik="0000001")}
        opened = []

        def fake_get_text(url, params=None):
            opened.append(url.split("/")[-1])
            return served[url.split("/")[-1]]
        submissions = {"filings": {"recent": recent, "files": [{"name": "p1.json"}]}}
        asked = []

        def fake_get(url, params=None):
            name = url.split("/")[-1]
            if name.startswith("CIK"):
                return submissions
            asked.append(name)
            return older
        with patch.object(sec_client, "_get", side_effect=fake_get), \
             patch.object(sec_client, "_get_text", side_effect=fake_get_text):
            execs = sec_insiders.fetch_executives("320193")
        assert sorted(e["name"] for e in execs) == ["James Crown", "James Dimon"]
        assert asked == ["p1.json"]
        # the page's Form 3 from before the window dates the seat, opened once
        assert next(e for e in execs if e["name"] == "James Dimon")["since"] == "2006-01-01"
        assert opened.count("o3.xml") == 1

    def test_an_inline_index_that_covers_the_window_reads_no_page_for_it(self):
        from app.scraper.sec_insiders import _window_pages
        subs = {"filings": {"files": [{"name": "p1.json"}]}}
        assert _window_pages(subs, ["2026-10-29", "2025-01-01"], "2025-07-30") == {}
        assert _window_pages(subs, ["2026-10-29"], None) == {}

    def test_the_window_counts_from_the_newest_form_3_or_4(self):
        from app.scraper.sec_insiders import FORM4_WINDOW_DAYS, _form34_window_start
        assert FORM4_WINDOW_DAYS == 456
        assert _form34_window_start(["8-K", "4", "3"], ["2026-10-30", "2026-10-29", "2026-01-01"]) == "2025-07-30"
        assert _form34_window_start(["8-K"], ["2026-10-30"]) is None
        assert _form34_window_start(["4"], ["not a date"]) is None


class TestDeparturesFromEightK:
    """A departing insider files nothing; the company announces it in an 8-K,
    Item 5.02. The submissions index says which 8-Ks carry the item; the text
    is prose, read only for names we already list. Both sentences below are
    the real filings (Apple 2026-04-20, Microsoft 2026-06-05).
    """

    COOK = ("On April 20, 2026, Apple Inc. (“Apple”) announced that Tim Cook will transition "
            "from his role as Chief Executive Officer to Executive Chair of Apple’s Board of "
            "Directors (the “Board”), effective September 1, 2026 (the “Transition Date”). "
            "On April 17, 2026, the Board appointed John Ternus, Apple’s Senior Vice President "
            "of Hardware Engineering, as Chief Executive Officer and a member of the Board, in "
            "each case effective on the Transition Date. Mr. Ternus, 50, joined Apple in 2001.")
    HOFFMAN = ("(b) On June 2, 2026, Reid Hoffman, a member of the Board of Directors of "
               "Microsoft Corporation (the “Company”) since 2017, informed the Company of his "
               "decision not to stand for re-election at the Company’s 2026 annual shareholder "
               "meeting (the “Annual Meeting”). Mr. Hoffman will continue to serve as a director "
               "until the Annual Meeting.")

    def test_an_effective_date_closes_the_named_seat(self):
        from app.scraper.sec_insiders import _departures_in_text
        out = _departures_in_text(self.COOK, ["Timothy D Cook", "John Ternus"], "2026-04-20")
        assert out == [{"name": "Timothy D Cook", "until": "2026-09-01", "role": "CEO",
                        "sentence": out[0]["sentence"]}]

    def test_a_notice_without_a_date_does_not_close_anything(self):
        # Hoffman still serves until a meeting whose date the text does not give.
        from app.scraper.sec_insiders import _departures_in_text
        assert _departures_in_text(self.HOFFMAN, ["Reid Hoffman"], "2026-06-05") == []

    def test_a_past_departure_is_dated_by_its_sentence(self):
        from app.scraper.sec_insiders import _departures_in_text
        text = ("On March 3, 2026, Jane Doe resigned as Chief Financial Officer of the Company. "
                "Mr. Roe passed away on February 1, 2026.")
        out = _departures_in_text(text, ["Jane Doe", "Richard Roe"], "2026-03-05")
        assert {(d["name"], d["until"], d["role"]) for d in out} == {
            ("Jane Doe", "2026-03-03", None), ("Richard Roe", "2026-02-01", None)}

    def test_a_past_departure_without_any_date_takes_the_filing_date(self):
        from app.scraper.sec_insiders import _departures_in_text
        out = _departures_in_text("Ms. Doe resigned as a director.", ["Jane Doe"], "2026-03-05")
        assert out and out[0]["until"] == "2026-03-05"

    def test_a_succession_closes_only_the_person_after_the_verb(self):
        # Both real Apple sentences (2026-01-02 and 2025-12-05). Borders and
        # Newstead are the successors and must stay open.
        from app.scraper.sec_insiders import _departures_in_text
        kondo = "Mr. Borders succeeds Chris Kondo in the role of Principal Accounting Officer."
        out = _departures_in_text(kondo, ["Ben Borders", "Chris Kondo"], "2026-01-02")
        assert [(d["name"], d["until"], d["role"]) for d in out] == \
            [("Chris Kondo", "2026-01-02", "Principal Accounting Officer")]
        adams = ("Apple announced that Jennifer Newstead will become Apple’s general counsel on "
                 "March 1, 2026, following a transition of duties from Kate Adams, who has served "
                 "as Apple’s general counsel since 2017.")
        out = _departures_in_text(adams, ["Jennifer Newstead", "Katherine L Adams"], "2025-12-05")
        assert [(d["name"], d["until"]) for d in out] == [("Katherine L Adams", "2026-03-01")]

    def test_a_surname_alone_never_matches(self):
        # One 8-K names several people; "Cook" in a sentence about someone else
        # must not close Tim Cook's seat.
        from app.scraper.sec_insiders import _departures_in_text
        text = "On May 1, 2026, the Company's head of Cook County operations resigned."
        assert _departures_in_text(text, ["Timothy D Cook"], "2026-05-02") == []

    def test_the_item_is_cut_out_of_the_whole_filing_without_its_heading(self):
        from app.scraper.sec_insiders import _item_502_text
        html = ("<p>Item 2.02 Results.</p><p>Item 5.02 Departure of Directors. "
                + self.COOK + "</p><p>Item 9.01 Exhibits.</p><p>SIGNATURES</p>")
        text = _item_502_text(html)
        assert text.startswith("On April 20, 2026") and "Exhibits" not in text
        full = ("<p>Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
                "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. "
                "(b) On June 2, 2026, Jane Doe resigned.</p><p>SIGNATURES</p>")
        assert _item_502_text(full) == "(b) On June 2, 2026, Jane Doe resigned."

    # Berkshire's 8-K of 2025-05-08 (accession 0001193125-25-115695), as the
    # reader saw it: the heading glued to paragraph b), whose "Departure"
    # closed Abel's CEO seat at the day it began, 2026-01-01 (dev, 2026-10-25).
    BERKSHIRE = ("Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain "
                 "Officers; Compensatory Arrangements of Certain Officers. b) On May 3, 2025, Mr. Warren "
                 "Buffett, Berkshire’s Chairman and CEO, announced that he was going to recommend to "
                 "Berkshire’s Board of Directors at its meeting to be held on May 4, 2025 that Mr. Greg Abel, "
                 "Berkshire’s Vice Chairman – Non-Insurance Operations, be appointed Berkshire’s Chief "
                 "Executive Officer to become effective on January 1, 2026. c) On May 4, 2025, Berkshire’s "
                 "Board of Directors voted unanimously to appoint Mr. Abel to become Berkshire’s President "
                 "and CEO effective on January 1, 2026. Mr. Abel, age 62, has been a director of the "
                 "Corporation and the Corporation’s Vice Chairman – Non-Insurance Operations since 2018.")

    def test_the_heading_s_departure_closes_nobody(self):
        from app.scraper.sec_insiders import _ITEM_502_HEADING_RX, _departures_in_text
        text = _ITEM_502_HEADING_RX.sub("", self.BERKSHIRE)
        assert text.startswith("b) On May 3, 2025")
        # an appointment is not a departure: nobody in this 8-K leaves
        assert _departures_in_text(text, ["Gregory E Abel", "Warren E Buffett"], "2025-05-08") == []

    def test_a_lettered_paragraph_starts_its_own_sentence(self):
        # "2026. b) Ms. Doe …" — a lowercase enumerator after a period used to
        # glue the appointment onto the resignation before it.
        from app.scraper.sec_insiders import _departures_in_text
        text = ("a) Mr. Roe resigned effective March 1, 2026. b) Ms. Doe was appointed Chief "
                "Financial Officer effective March 1, 2026. (c) Mr. Poe was elected a director.")
        out = _departures_in_text(text, ["Richard Roe", "Jane Doe", "Edgar Poe"], "2026-03-05")
        assert [(d["name"], d["until"]) for d in out] == [("Richard Roe", "2026-03-01")]

    def test_end_to_end_only_8ks_with_item_502_are_opened(self):
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders
        submissions = {"filings": {"recent": {
            "form":            ["8-K", "8-K", "10-Q"],
            "items":           ["5.02", "2.02,9.01", ""],
            "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000003"],
            "primaryDocument": ["a.htm", "b.htm", "c.htm"],
            "filingDate":      ["2026-04-20", "2026-05-01", "2026-05-02"],
        }}}
        opened = []

        def fake_get_text(url, params=None):
            opened.append(url.split("/")[-1])
            return "<p>Item 5.02 Departure. " + self.COOK + "</p><p>SIGNATURE</p>"
        with patch.object(sec_client, "_get", return_value=submissions), \
             patch.object(sec_client, "_get_text", side_effect=fake_get_text):
            out = sec_insiders.fetch_departures("320193", ["Timothy D Cook"])
        assert opened == ["a.htm"]
        assert len(out) == 1 and out[0]["until"] == "2026-09-01" and out[0]["role"] == "CEO"
        assert out[0]["source_date"] == "2026-04-20"
        assert "0000000001-26-000001" in out[0]["source_url"]

    def test_nobody_to_look_for_costs_no_request(self):
        from unittest.mock import patch
        from app.scraper import sec_client, sec_insiders
        with patch.object(sec_client, "_get", side_effect=AssertionError("must not fetch")):
            assert sec_insiders.fetch_departures("320193", []) == []


class TestForm5InTheInsiderWindow:
    """A director whose newest filing about the issuer is a Form 5 — the annual
    statement of what needed no Form 4 — was not listed: the window read Forms
    3 and 4 only. WesBanco's Michael J Crawford (Form 5 of 2026-02-10,
    isDirector) had no seat (findings item 119)."""

    @staticmethod
    def _xml(owner, cik, doc_type, period="2025-12-31"):
        return (f'<?xml version="1.0"?><ownershipDocument><documentType>{doc_type}</documentType>'
                f'<periodOfReport>{period}</periodOfReport>'
                f'<issuer><issuerCik>0000203596</issuerCik></issuer>'
                f'<reportingOwner><reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik>'
                f'<rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>'
                f'<reportingOwnerRelationship><isDirector>1</isDirector>'
                f'</reportingOwnerRelationship></reportingOwner></ownershipDocument>')

    def _window(self, forms, docs):
        from app.scraper import sec_client, sec_insiders
        n = len(forms)
        submissions = {"filings": {"recent": {
            "form": [f for f, _, _ in forms],
            "accessionNumber": [f"0000000001-26-{i:06d}" for i in range(n)],
            "primaryDocument": [d for _, d, _ in forms],
            "filingDate": [day for _, _, day in forms]}}}
        with patch.object(sec_client, "_get", return_value=submissions), \
             patch.object(sec_client, "_get_text", side_effect=lambda url, params=None: docs[url.rsplit("/", 1)[-1]]):
            return sec_insiders.fetch_executives("203596")

    def test_a_director_who_files_form_5_is_listed(self):
        execs = self._window(
            [("5", "f5.xml", "2026-02-10"), ("4", "f4.xml", "2026-01-15")],
            {"f5.xml": self._xml("CRAWFORD MICHAEL J", "0001234567", "5"),
             "f4.xml": self._xml("CLOSSIN TODD", "0001111111", "4")})
        assert {(e["name"], e["document_type"]) for e in execs} == {("Michael J Crawford", "5"), ("Todd Clossin", "4")}
        crawford = next(e for e in execs if e["document_type"] == "5")
        assert crawford["role"] == "Director" and crawford["source_date"] == "2026-02-10"
        assert crawford["since"] is None            # a Form 5 dates no seat; the Form 3 does

    def test_an_amended_form_5_counts_too(self):
        execs = self._window([("5/A", "f5a.xml", "2026-03-01")],
                             {"f5a.xml": self._xml("CRAWFORD MICHAEL J", "0001234567", "5/A")})
        assert [e["name"] for e in execs] == ["Michael J Crawford"]

    def test_their_form_3_still_dates_the_seat(self):
        execs = self._window(
            [("5", "f5.xml", "2026-02-10"), ("3", "f3.xml", "2018-08-22")],
            {"f5.xml": self._xml("CRAWFORD MICHAEL J", "0001234567", "5"),
             "f3.xml": self._xml("CRAWFORD MICHAEL J", "0001234567", "3", period="2018-08-20")})
        assert [(e["name"], e["since"]) for e in execs] == [("Michael J Crawford", "2018-08-20")]

    def test_a_form_5_alone_opens_the_window(self):
        # the window is counted back from the newest insider filing — a Form 5 is one
        from app.scraper import sec_insiders
        assert sec_insiders._form34_window_start(["10-K", "5"], ["2026-03-01", "2026-02-10"]) is not None
        assert sec_insiders._form34_window_start(["10-K", "8-K"], ["2026-03-01", "2026-02-10"]) is None

    def test_a_form_5_on_an_older_submissions_page_is_inside_the_window_too(self):
        from app.scraper import sec_client, sec_insiders
        recent = {"form": ["4"], "accessionNumber": ["0000000001-26-000001"],
                  "primaryDocument": ["f4.xml"], "filingDate": ["2026-03-01"]}
        older = {"form": ["5", "10-K"], "accessionNumber": ["0000000001-26-000002", "0000000001-25-000003"],
                 "primaryDocument": ["f5.xml", "k.htm"], "filingDate": ["2026-02-10", "2024-01-01"]}
        docs = {"f4.xml": self._xml("CLOSSIN TODD", "0001111111", "4"),
                "f5.xml": self._xml("CRAWFORD MICHAEL J", "0001234567", "5")}

        def get(url, params=None):
            return older if url.endswith("older.json") else {
                "filings": {"recent": recent, "files": [{"name": "older.json"}]}}
        with patch.object(sec_client, "_get", side_effect=get), \
             patch.object(sec_client, "_get_text", side_effect=lambda url, params=None: docs[url.rsplit("/", 1)[-1]]):
            execs = sec_insiders.fetch_executives("203596")
        assert sorted(e["name"] for e in execs) == ["Michael J Crawford", "Todd Clossin"]

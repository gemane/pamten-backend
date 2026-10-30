"""Is a GLEIF record and an SEC filer one legal entity? (`legal_identity`,
findings item 4). The pairs are the 2026-10-29b rebuild's, their facts as
GLEIF's API and EDGAR's submissions gave them on 2026-10-30."""
import pytest

from app.scraper.legal_identity import (JOIN, REFUSE, Evidence, Filer, _in_force, header_address,
                                        names_at, reference_day, same_legal_entity)

TODAY = "2026-10-30"

NYT_GLEIF = {"name": "New York Times Co.", "lei_id": "529900J1WEMMIW7BOH57", "country": "US",
             "jurisdiction_code": None, "hq_street": "620 Eighth Avenue", "hq_city": "New York",
             "gleif_registration_status": "LAPSED", "validation_sources": "ENTITY_SUPPLIED_ONLY",
             "gleif_last_update": "2021-07-16"}
NYT = Filer("0000071691", "NEW YORK TIMES CO", home="US-NY", street="620 EIGHTH AVENUE", city="NEW YORK")
TENCENT_GLEIF = {"name": "騰訊控股有限公司", "other_names": ["Tencent Holdings Limited"],
                 "lei_id": "254900N4SLUMW4XUYY11", "country": "KY", "gleif_registration_status": "ISSUED",
                 "validation_sources": "FULLY_CORROBORATED", "gleif_last_update": "2026-01-09"}
TENCENT = Filer("0001293451", "Tencent Holdings Ltd", home="KY")
DOMINOS_INC_GLEIF = {"name": "Domino's Pizza, Inc.", "lei_id": "25490005ZWM1IF9UXU57", "country": "US",
                     "jurisdiction_code": "US-DE", "gleif_registration_status": "ISSUED",
                     "validation_sources": "FULLY_CORROBORATED", "gleif_last_update": "2026-04-07"}
DOMINOS_LLC_GLEIF = {**DOMINOS_INC_GLEIF, "name": "Domino's Pizza LLC", "lei_id": "254900BIRZMWFO44TO04",
                     "jurisdiction_code": "US-MI"}
DOMINOS = Filer("0001286681", "DOMINOS PIZZA INC", home="US-DE")
#: GLEIF: the LLC is ultimately consolidated by the Inc. since 1999-10-27, no end
LLC_PARENT = {"lei": "25490005ZWM1IF9UXU57", "names": ["Domino's Pizza, Inc."], "place": "US-DE",
              "status": "ACTIVE", "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "1999-10-27T00:00:00Z"}]}


def fake(address=None, children=(), parents=(), held=()):
    asked = []

    def address_at(cik, day):
        asked.append(day)
        return address
    ev = Evidence(address_at=address_at, gleif_children=lambda lei: list(children),
                  gleif_parents=lambda lei: list(parents), held_at=lambda cik, day: list(held))
    ev.asked = asked
    return ev


class TestTheFourPairs:
    def test_nyt_joins_on_its_address_at_the_records_last_update(self):
        ev = fake(address=("620 EIGHTH AVENUE", "NEW YORK", None))
        assert same_legal_entity(NYT_GLEIF, NYT, today=TODAY, evidence=ev) == (
            JOIN, "name+country+address@2021-07-16")
        assert ev.asked == ["2021-07-16"]            # D, not today

    def test_tencent_joins_on_its_transliterated_name_and_place(self):
        assert same_legal_entity(TENCENT_GLEIF, TENCENT, today=TODAY, evidence=fake()) == (JOIN, "name+place")

    def test_dominos_inc_joins(self):
        assert same_legal_entity(DOMINOS_INC_GLEIF, DOMINOS, today=TODAY, evidence=fake()) == (JOIN, "name+place")

    def test_dominos_llc_is_refused_on_its_legal_form(self):
        verdict, why = same_legal_entity(DOMINOS_LLC_GLEIF, DOMINOS, today=TODAY, evidence=fake())
        assert verdict == REFUSE and "legal name differs" in why

    def test_dominos_llc_spelled_as_the_inc_is_refused_on_its_place(self):
        node = {**DOMINOS_LLC_GLEIF, "name": "Domino's Pizza, Inc."}
        assert same_legal_entity(node, DOMINOS, today=TODAY, evidence=fake()) == (
            REFUSE, "registered US-MI, filer US-DE")


class TestTheVeto:
    def test_an_open_link_to_a_record_that_is_the_filer_vetoes(self):
        # even a record that passes 1 and 2 is no join while it is the filer's child
        node = {**DOMINOS_LLC_GLEIF, "name": "Domino's Pizza, Inc.", "jurisdiction_code": "US-DE"}
        assert same_legal_entity(node, DOMINOS, today=TODAY, evidence=fake(parents=[LLC_PARENT])) == (
            REFUSE, "child of 25490005ZWM1IF9UXU57 since 1999-10-27")

    def test_a_link_that_ended_before_d_and_today_does_not(self):
        ended = {**LLC_PARENT, "status": "INACTIVE",
                 "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "1999-10-27", "endDate": "2010-01-01"}]}
        assert same_legal_entity(DOMINOS_INC_GLEIF, DOMINOS, today=TODAY,
                                 evidence=fake(parents=[ended]))[0] == JOIN

    def test_a_parent_that_is_not_the_filer_does_not(self):
        other = {**LLC_PARENT, "names": ["Bain Capital LP"], "place": "US-DE"}
        assert same_legal_entity(DOMINOS_INC_GLEIF, DOMINOS, today=TODAY, evidence=fake(parents=[other]))[0] == JOIN


class TestAWeakRecord:
    def test_a_lapsed_record_whose_filer_has_moved_since_still_joins_on_the_old_address(self):
        moved = Filer(NYT.cik, NYT.name, home="US-NY", street="1 New Street", city="Brooklyn")
        ev = fake(address=("620 Eighth Ave", "New York", None))       # the 2021 header
        assert same_legal_entity(NYT_GLEIF, moved, today=TODAY, evidence=ev)[0] == JOIN

    def test_an_address_matching_only_today_is_not_enough(self):
        ev = fake(address=("1 Somewhere Else", "Albany", None))      # where it was at D
        verdict, why = same_legal_entity(NYT_GLEIF, NYT, today=TODAY, evidence=ev)
        assert verdict == REFUSE and "weak record" in why and "2021-07-16" in why

    def test_no_address_but_a_subsidiary_held_at_d(self):
        node = {**NYT_GLEIF, "hq_street": None, "hq_city": None}
        child = {"name": "NYT Capital, LLC", "place": "US-DE", "status": "ACTIVE",
                 "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "2019-01-03"}]}
        ev = fake(children=[child], held=[("NYT Capital, LLC", "Delaware")])
        assert same_legal_entity(node, NYT, today=TODAY, evidence=ev) == (
            JOIN, "name+country+subsidiary@2021-07-16 (NYT Capital, LLC)")

    def test_a_child_whose_link_ended_before_d_is_no_support(self):
        node = {**NYT_GLEIF, "hq_street": None, "hq_city": None}
        child = {"name": "NYT Capital, LLC", "place": "US-DE", "status": "INACTIVE",
                 "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "2019-01-03", "endDate": "2020-01-01"}]}
        ev = fake(children=[child], held=[("NYT Capital, LLC", "Delaware")])
        assert same_legal_entity(node, NYT, today=TODAY, evidence=ev)[0] == REFUSE

    def test_a_child_held_in_another_country_is_no_support(self):
        node = {**NYT_GLEIF, "hq_street": None, "hq_city": None}
        child = {"name": "NYT Capital, LLC", "place": "US-DE", "status": "ACTIVE", "periods": []}
        ev = fake(children=[child], held=[("NYT Capital, LLC", "Japan")])
        assert same_legal_entity(node, NYT, today=TODAY, evidence=ev)[0] == REFUSE

    def test_a_maintained_self_reported_record_is_compared_today(self):
        node = {**DOMINOS_INC_GLEIF, "validation_sources": "ENTITY_SUPPLIED_ONLY",
                "hq_street": "30 Frank Lloyd Wright Drive", "hq_city": "Ann Arbor"}
        ev = fake(address=("30 FRANK LLOYD WRIGHT DR", "ANN ARBOR", None))     # postcode aside
        assert same_legal_entity(node, DOMINOS, today=TODAY, evidence=ev) == (
            JOIN, f"name+place+address@{TODAY}")
        assert ev.asked == [TODAY]


class TestNamesAndPlaces:
    def test_a_renamed_company_joins_on_the_name_in_force_at_d(self):
        # Meta Platforms was "Facebook, Inc." until 2021-10-28 (EDGAR formerNames)
        meta = Filer("0001326801", "Meta Platforms, Inc.", former_names=("Facebook Inc|2021-10-28",),
                     home="US-DE")
        lapsed = {"name": "Facebook, Inc.", "lei_id": "BQ4BKCS1HXDV9HN80Z93", "country": "US",
                  "jurisdiction_code": "US-DE", "gleif_registration_status": "LAPSED",
                  "gleif_last_update": "2021-05-01", "hq_street": "1601 Willow Road", "hq_city": "Menlo Park"}
        ev = fake(address=("1601 WILLOW ROAD", "MENLO PARK", None))
        assert same_legal_entity(lapsed, meta, today=TODAY, evidence=ev)[0] == JOIN
        # the old name after it ended is no match
        later = {**lapsed, "gleif_last_update": "2023-01-01"}
        assert same_legal_entity(later, meta, today=TODAY, evidence=ev)[0] == REFUSE

    def test_a_namesake_registered_abroad_is_refused(self):
        perfect = Filer("0001899830", "Perfect Corp.", home="KY")
        japan = {"name": "Perfect Corp.", "lei_id": "X", "country": "JP", "gleif_registration_status": "ISSUED"}
        assert same_legal_entity(japan, perfect, today=TODAY, evidence=fake()) == (
            REFUSE, "registered JP, filer KY")

    def test_names_at(self):
        f = Filer("1", "Meta Platforms, Inc.", former_names=("FACEBOOK INC|2021-10-28", "TheFacebook|2005-09-20"))
        assert names_at(f, "2021-05-01") == ["Meta Platforms, Inc.", "FACEBOOK INC"]
        assert names_at(f, "2004-01-01") == ["Meta Platforms, Inc.", "TheFacebook"]
        assert names_at(f, "2022-01-01") == ["Meta Platforms, Inc."]

    def test_reference_day(self):
        assert reference_day(NYT_GLEIF, TODAY) == "2021-07-16"
        assert reference_day(DOMINOS_INC_GLEIF, TODAY) == TODAY
        assert reference_day({**NYT_GLEIF, "gleif_last_update": None}, TODAY) == TODAY

    @pytest.mark.parametrize("periods, status, day, expected", [
        ([{"type": "RELATIONSHIP_PERIOD", "startDate": "1999-10-27T00:00:00Z"}], "ACTIVE", "2021-01-01", True),
        ([{"type": "RELATIONSHIP_PERIOD", "startDate": "2022-01-01"}], "ACTIVE", "2021-01-01", False),
        ([{"type": "RELATIONSHIP_PERIOD", "startDate": "2000-01-01", "endDate": "2020-12-31"}], "INACTIVE",
         "2021-01-01", False),
        ([{"type": "ACCOUNTING_PERIOD", "startDate": "2023-01-01", "endDate": "2023-12-31"}], "ACTIVE",
         "2010-01-01", True),                                   # no relationship period: the status says
        ([], "INACTIVE", "2021-01-01", False),
    ])
    def test_in_force(self, periods, status, day, expected):
        assert _in_force(periods, status, day) is expected


class TestTheFilingHeader:
    SGML = """<SEC-HEADER>
<REPORTING-OWNER>
<OWNER-DATA>
<CONFORMED-NAME>GOLDEN ARTHUR S.
<CIK>0001838537
</OWNER-DATA>
<MAIL-ADDRESS>
<STREET1>THE NEW YORK TIMES COMPANY
<STREET2>620 EIGHTH AVENUE
<CITY>NEW YORK
</MAIL-ADDRESS>
</REPORTING-OWNER>
<ISSUER>
<COMPANY-DATA>
<CONFORMED-NAME>NEW YORK TIMES CO
<CIK>0000071691
</COMPANY-DATA>
<BUSINESS-ADDRESS>
<STREET1>620 EIGHTH AVENUE
<CITY>NEW YORK
<STATE>NY
<ZIP>10018
</BUSINESS-ADDRESS>
</ISSUER>
</SEC-HEADER>"""

    def test_the_filers_own_business_address(self):
        assert header_address(self.SGML, "71691") == ("620 EIGHTH AVENUE", "NEW YORK")

    def test_not_the_reporting_owners_and_not_another_ciks(self):
        assert header_address(self.SGML, "1838537") is None
        assert header_address(self.SGML, "320193") is None

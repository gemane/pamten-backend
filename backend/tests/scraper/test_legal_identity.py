"""Is a GLEIF record and an SEC filer one legal entity? (`legal_identity`,
findings item 4). The pairs are the 2026-10-29b rebuild's, their facts as
GLEIF's API and EDGAR's submissions gave them on 2026-10-30."""
import pytest

from unittest.mock import patch

from app.scraper.legal_identity import (JOIN, REFUSE, Evidence, Filer, _in_force, filer_held_at,
                                        header_address, names_at, reference_day, same_legal_entity)

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


class TestTheFormFromGleifsCode:
    """GLEIF's legal name without its form, the form as an ELF code (findings
    item 19, 2026-10-30): AB InBev's record says "ANHEUSER-BUSCH INBEV", R85P."""
    ABI_GLEIF = {"name": "ANHEUSER-BUSCH INBEV", "lei_id": "5493008H3828EMEXB082", "legal_form_code": "R85P",
                 "country": "BE", "jurisdiction_code": None, "gleif_registration_status": "ISSUED",
                 "validation_sources": "FULLY_CORROBORATED", "gleif_last_update": "2026-06-01"}

    @pytest.mark.parametrize("edgar", ["Anheuser-Busch InBev SA/NV", "ANHEUSER-BUSCH INBEV NV",
                                       "Anheuser-Busch InBev S.A."])
    def test_the_codes_abbreviation_is_the_form(self, edgar):
        assert same_legal_entity(self.ABI_GLEIF, Filer("0001668717", edgar, home="BE"),
                                 today=TODAY, evidence=fake()) == (JOIN, "name(form R85P)+place")

    @pytest.mark.parametrize("edgar", ["Anheuser-Busch InBev Inc.", "Anheuser-Busch InBev SA/NV Holdings"])
    def test_another_form_or_word_is_another_name(self, edgar):
        verdict, why = same_legal_entity(self.ABI_GLEIF, Filer("1", edgar, home="BE"), today=TODAY, evidence=fake())
        assert verdict == REFUSE and "legal name differs" in why

    def test_no_code_no_form(self):
        node = {**self.ABI_GLEIF, "legal_form_code": None}
        assert same_legal_entity(node, Filer("1", "Anheuser-Busch InBev SA/NV", home="BE"),
                                 today=TODAY, evidence=fake())[0] == REFUSE

    def test_a_gleif_name_with_its_form_is_compared_as_it_is(self):
        # "Domino's Pizza LLC" names its form; the code adds nothing to it
        node = {**DOMINOS_LLC_GLEIF, "legal_form_code": "XTIQ"}
        assert same_legal_entity(node, DOMINOS, today=TODAY, evidence=fake())[0] == REFUSE


class TestTheExhibitAtD:
    """The list a filer held on D: the report covering D, else the newest filed
    by D. A maintained record's D is today, which no report covers yet; until
    2026-10-30 no list was read for it (findings item 19)."""
    FILINGS = [("20-F", "a26", "2026-03-30", "2025-12-31"), ("20-F", "a25", "2025-03-28", "2024-12-31"),
               ("20-F", "a24", "2024-03-27", "2023-12-31")]                          # newest first

    def _picked(self, day):
        picked = []
        with patch("app.database.db.get_session"), \
             patch("app.scraper.sec_ex21.annual_filings", return_value=self.FILINGS), \
             patch("app.scraper.sec_ex21.exhibit_candidates", side_effect=lambda cik, form, acc, *a: picked.append(acc) or []), \
             patch("app.scraper.sec_ex21._parse_first", return_value=([{"name": "Yabora", "jurisdiction": "Brazil"}],)):
            held = filer_held_at("0001355444", day)
        return picked, held

    def test_today_reads_the_newest_report(self):
        assert self._picked("2026-10-30") == (["a26"], [("Yabora", "Brazil")])

    def test_a_past_day_reads_the_report_covering_it(self):
        assert self._picked("2024-06-30")[0] == ["a25"]           # FY2024, filed 2025

    def test_a_day_before_every_report_reads_the_oldest(self):
        assert self._picked("2020-01-01")[0] == ["a24"]           # the oldest covers it


class TestTheFilersOffice:
    def test_today_s_office_is_the_report_s(self):
        from app.scraper import legal_identity
        sub = {"name": "MERCADOLIBRE INC", "addresses": {"business": {"street1": "OLD 1", "city": "OLDTOWN"}}}
        with patch("app.scraper.sec_edgar._submissions", return_value=sub), \
             patch("app.scraper.sec_edgar.stated_headquarters",
                   return_value={"street": "WTC Free Zone Dr. Luis Bonavita 1294", "city": "Montevideo"}):
            facts = legal_identity.filer_facts("0001099590")
        assert (facts.street, facts.city) == ("WTC Free Zone Dr. Luis Bonavita 1294", "Montevideo")


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


class TestPreviousNamesAreNotNamesInForce:
    """Findings item 16a: GLEIF's previous legal names no longer sit in
    other_names, so point 1 compares current names only."""

    def test_a_filer_named_only_like_a_gleif_previous_name_is_refused(self):
        parent = {"name": "BlackRock, Inc.", "lei_id": "529900VBK42Y5HHRMD23", "country": "US",
                  "jurisdiction_code": "US-DE", "gleif_registration_status": "ISSUED",
                  "gleif_former_names": ["BLACKROCK FUNDING, INC.|2024-09-30"]}
        funding = Filer("0009999999", "BLACKROCK FUNDING INC", home="US-DE")
        verdict, why = same_legal_entity(parent, funding, today=TODAY, evidence=fake())
        assert verdict == REFUSE and "legal name differs" in why

    def test_tencent_still_joins_on_its_transliteration(self):
        assert same_legal_entity(TENCENT_GLEIF, TENCENT, today=TODAY, evidence=fake())[0] == JOIN


class TestACorporatePscAndItsGleifRecord:
    """A foreign owner as Companies House records it, against GLEIF: the
    legal name with its form, the place, and the building must all agree
    (findings item 25: Berkshire Hathaway)."""

    GLEIF = {"name": "BERKSHIRE HATHAWAY INC", "lei_id": "5493000C01ZX7D35SD85", "jurisdiction_code": "US-DE",
             "country": "US", "reg_street": "C/O THE CORPORATION TRUST COMPANY, 1209 ORANGE ST",
             "reg_city": "WILMINGTON", "reg_postcode": "19801",
             "hq_street": "3555 Farnam Street, Suite 1440", "hq_city": "Omaha", "hq_postcode": "68131"}
    PSC = {"id": "chpsc:03230337:x", "name": "Berkshire Hathaway Inc.", "country": "US", "jurisdiction_code": "US-DE",
           "reg_street": "3555 Farnam Street", "reg_city": "Omaha", "reg_postcode": "68131"}

    def _verdict(self, gleif=None, **psc):
        from app.scraper.legal_identity import psc_same_legal_entity
        return psc_same_legal_entity({**self.GLEIF, **(gleif or {})}, {**self.PSC, **psc})

    def test_name_place_and_headquarters(self):
        assert self._verdict() == (JOIN, "name+place+hq")

    def test_the_registered_address_counts_too(self):
        assert self._verdict(reg_street="1209 Orange Street", reg_city="Wilmington", reg_postcode="19801") == \
            (JOIN, "name+place+address")

    def test_a_country_alone_does_not_contradict(self):
        # the PSC node as imported before its legal authority was kept, and
        # its town still in the street line: the postcode carries the building
        assert self._verdict(jurisdiction_code=None, reg_street="3555 Farnam Street, Omaha",
                             reg_city="Nebraska") == (JOIN, "name+country+hq")

    def test_another_legal_form_is_another_entity(self):
        assert self._verdict(name="Berkshire Hathaway LLC")[0] == REFUSE

    def test_another_place_is_another_entity(self):
        assert self._verdict(jurisdiction_code="US-NV") == (REFUSE, "registered US-DE, filer US-NV")
        assert self._verdict(jurisdiction_code=None, country="KY")[0] == REFUSE

    def test_another_building_is_no_join(self):
        assert self._verdict(reg_street="3556 Farnam Street") == (REFUSE, "the address differs")
        assert self._verdict(reg_street="3555 Dodge Street") == (REFUSE, "the address differs")
        assert self._verdict(reg_street=None) == (REFUSE, "the address differs")

    def test_the_same_building_in_another_town_is_no_join(self):
        assert self._verdict(reg_city="Lincoln", reg_postcode="68508")[0] == REFUSE

    def test_the_form_from_gleifs_code(self):
        abi = {"name": "ANHEUSER-BUSCH INBEV", "legal_form_code": "R85P", "jurisdiction_code": None, "country": "BE",
               "hq_street": "Brouwerijplein 1", "hq_city": "Leuven", "hq_postcode": "3000"}
        assert self._verdict(abi, name="Anheuser-Busch InBev SA/NV", jurisdiction_code=None, country="BE",
                             reg_street="Brouwerijplein 1", reg_city="Leuven", reg_postcode="3000")[0] == JOIN

    def test_an_entity_born_after_the_notice_is_another_one(self):
        # a new holding company took the old name (Apollo 2022, KKR 2021): the
        # PSC notified before it names the old company
        assert self._verdict({"incorporated_on": "2021-07-01"}, notified_on="2019-03-04") == \
            (REFUSE, "incorporated 2021-07-01, after the PSC notice of 2019-03-04")
        assert self._verdict({"incorporated_on": "2019-03-04"}, notified_on="2019-03-04")[0] == JOIN
        assert self._verdict({"incorporated_on": "1998-12-21"}, notified_on="2019-03-04")[0] == JOIN
        assert self._verdict({"incorporated_on": "2021-07-01"})[0] == JOIN, "no notice date, no guard"

"""A register writes the company a knowledge base points at (2026-10-29).

Wikidata's LEI / CIK are a pointer: GLEIF's API or EDGAR writes the node, and
a node whose register id the item contradicts gets nothing of it. The fixture
is GLEIF's API record for BlackRock Finance, Inc. (549300LRIF3NWCU26A80) — the
pre-2024 BlackRock, whose ids Wikidata's BlackRock item still states.
"""
import json
import pathlib
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.scraper import register_pointer as rp

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "gleif_api_blackrock_finance.json"


def _api():
    return json.loads(FIXTURE.read_text())


class TestTheGoldenCopyShape:
    def test_the_bulk_importers_reading_writes_the_api_record(self):
        from app.scraper.gleif_lei_cdf import _entity_props
        node_id, props = _entity_props(rp.golden_copy_record(_api()["data"]["attributes"]), "gleif", 92)
        assert node_id == "lei:549300LRIF3NWCU26A80" and props["lei_id"] == "549300LRIF3NWCU26A80"
        assert props["name"] == "BlackRock Finance, Inc."
        assert props["other_names"] == ["BLACKROCK, INC."]
        assert props["country"] == "US" and props["jurisdiction_code"] == "US-DE"
        assert props["registration_authority_code"] == "RA000602" and props["registration_number"] == "4109022"
        assert props["register_id"] == "RA000602:4109022"
        assert props["legal_form_code"] == "XTIQ"
        assert props["incorporated_on"] == "2006-02-13"          # 2006-02-12T23:00Z is a CET midnight
        assert props["lei_registration_date"] == "2014-02-20"
        assert props["gleif_registration_status"] == "ISSUED" and props["active"] is True
        assert props["validation_sources"] == "FULLY_CORROBORATED"
        assert props["gleif_last_update"] == "2026-10-28"
        assert props["name_credibility"] == 92
        assert props["hq_city"] == "New York" and props["hq_street"] == "50 HUDSON YARDS"
        assert props["reg_city"] == "WILMINGTON" and props["reg_postcode"] == "19808"
        assert props["address"].startswith("c/o CORPORATION SERVICE COMPANY")
        assert "dissolved_on" not in props

    @pytest.mark.parametrize("stamp,day", [
        ("2006-02-12T23:00:00Z", "2006-02-13"),       # midnight in Vienna
        ("2014-02-20T01:35:00Z", "2014-02-20"),
        ("2024-01-07T05:00:00Z", "2024-01-07"),       # midnight in New York
        ("2024-01-07", "2024-01-07"),
        (None, None),
    ])
    def test_an_api_instant_is_the_day_the_golden_copy_states(self, stamp, day):
        assert rp._api_day(stamp) == day


def _resp(status, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError("x", request=MagicMock(), response=r)
    return r


class TestFetching:
    def test_an_unknown_lei_is_none_and_a_failure_raises(self):
        with patch("httpx.get", return_value=_resp(404)):
            assert rp.fetch_gleif_record("ZZZZZZZZZZZZZZZZZZZZ") is None
        with patch("httpx.get", return_value=_resp(503)), pytest.raises(httpx.HTTPStatusError):
            rp.fetch_gleif_record("549300LRIF3NWCU26A80")
        with patch("httpx.get", return_value=_resp(200, _api())) as get:
            rec = rp.fetch_gleif_record("549300LRIF3NWCU26A80")
        assert rec["LEI"] == {"$": "549300LRIF3NWCU26A80"}
        assert "User-Agent" in get.call_args.kwargs["headers"]

    def test_an_unknown_cik_writes_nothing(self):
        err = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock(status_code=404))
        with patch("app.scraper.sec_edgar._submissions", side_effect=err), \
             patch("app.scraper.graph_writer._upsert_entity_by_name") as up:
            assert rp.write_from_edgar("0000000001") is None
        up.assert_not_called()

    def test_edgar_writes_the_filer_under_its_own_name(self):
        sub = {"name": "BlackRock Finance, Inc.", "stateOfIncorporation": "DE",
               "addresses": {"business": {"street1": "50 Hudson Yards", "city": "New York",
                                          "stateOrCountry": "NY", "zipCode": "10001"}}}
        with patch("app.scraper.sec_edgar._submissions", return_value=sub), \
             patch("app.scraper.runner._ensure_source", return_value="sec-edgar"), \
             patch("app.scraper.graph_writer._upsert_entity_by_name", return_value="sec:0001364742") as up:
            assert rp.write_from_edgar("0001364742") == "sec:0001364742"
        kw = up.call_args.kwargs
        assert kw["name"] == "BlackRock Finance, Inc." and kw["cik"] == "0001364742"
        assert kw["register_name"] is True and kw["source_id"] == "sec-edgar"

    def test_edgar_writes_the_former_names_with_their_days(self):
        # what lets `stale` see that an item pointing here describes the CIK's past
        sub = {"name": "BlackRock Finance, Inc.",
               "formerNames": [{"name": "BlackRock Inc.", "from": "2006-10-05T00:00:00.000Z",
                                "to": "2024-09-26T00:00:00.000Z"}]}
        with patch("app.scraper.sec_edgar._submissions", return_value=sub), \
             patch("app.scraper.runner._ensure_source", return_value="sec-edgar"), \
             patch("app.scraper.graph_writer._upsert_entity_by_name", return_value="sec:0001364742") as up:
            rp.write_from_edgar("0001364742")
        assert up.call_args.kwargs["former_names"] == ["BlackRock Inc.|2024-09-26"]


class TestFollowingThePointer:
    def test_gleif_writes_the_node_with_the_bulk_writer(self):
        written = []

        class Batch:
            def entity(self, node_id, props):
                written.append((node_id, props))

            def flush(self):
                written.append("flushed")
        with patch("app.merged_ids.canonical_id", side_effect=lambda i: i), \
             patch.object(rp, "fetch_gleif_record",
                          return_value=rp.golden_copy_record(_api()["data"]["attributes"])), \
             patch("app.scraper.runner._ensure_source", return_value="gleif"), \
             patch("app.scraper.bulk_import._BatchWriter", Batch), \
             patch.object(rp, "_join_filer", return_value=None):
            assert rp.write_from_gleif("549300LRIF3NWCU26A80") == "lei:549300LRIF3NWCU26A80"
        assert written[0][0] == "lei:549300LRIF3NWCU26A80" and written[0][1]["source_id"] == "gleif"
        assert written[-1] == "flushed"

    def test_a_merged_away_lei_answers_with_its_survivor_and_asks_nothing(self):
        with patch("app.merged_ids.canonical_id", return_value="lei:SURVIVOR"), \
             patch.object(rp, "fetch_gleif_record") as fetch:
            assert rp.write_from_gleif("549300LRIF3NWCU26A80") == "lei:SURVIVOR"
        fetch.assert_not_called()

    def test_an_lei_before_a_cik_and_the_next_when_one_is_unknown(self):
        with patch.object(rp, "_gleif_enabled", return_value=True), \
             patch.object(rp, "_sec_enabled", return_value=True), \
             patch.object(rp, "write_from_gleif", side_effect=[None, "lei:B"]) as gl, \
             patch.object(rp, "write_from_edgar") as ed:
            assert rp.write_from_pointer(["A" * 20, "B" * 20], ["0000000001"]) == "lei:B"
        assert gl.call_count == 2 and not ed.called
        with patch.object(rp, "_gleif_enabled", return_value=True), \
             patch.object(rp, "_sec_enabled", return_value=True), \
             patch.object(rp, "write_from_gleif", return_value=None), \
             patch.object(rp, "write_from_edgar", return_value="sec:0000000001"):
            assert rp.write_from_pointer(["A" * 20], ["0000000001"]) == "sec:0000000001"

    def test_gleif_follows_the_catalogue_switch_alone(self):
        # no environment flag besides it: the default settings must not
        # switch the lookup off (2026-10-29: no LEI ever wrote a node)
        with patch("app.scraper.sources.get_source_enabled", return_value=True) as sw:
            assert rp._gleif_enabled() is True
        sw.assert_called_once_with("bods_gleif")
        with patch("app.scraper.sources.get_source_enabled", return_value=False):
            assert rp._gleif_enabled() is False

    def test_a_register_that_is_switched_off_is_not_asked(self):
        with patch.object(rp, "_gleif_enabled", return_value=False), \
             patch.object(rp, "_sec_enabled", return_value=False), \
             patch.object(rp, "write_from_gleif") as gl, patch.object(rp, "write_from_edgar") as ed:
            assert rp.write_from_pointer(["A" * 20], ["0000000001"]) is None
        assert not gl.called and not ed.called


class TestContradiction:
    def test_another_register_id_is_a_contradiction(self):
        node = {"lei_id": "529900VBK42Y5HHRMD23", "sec_cik": "0002012383"}
        assert "LEI 529900VBK42Y5HHRMD23" in rp.contradiction(node, ["549300LRIF3NWCU26A80"], [])
        assert "CIK 0002012383" in rp.contradiction(node, [], ["0001364742"])

    def test_agreement_silence_and_a_bare_node_are_not(self):
        assert rp.contradiction({"lei_id": "B" * 20}, ["A" * 20, "B" * 20], []) is None
        assert rp.contradiction({"lei_id": "B" * 20}, [], ["0000000001"]) is None   # no CIK on the node
        assert rp.contradiction({}, ["A" * 20], ["0000000001"]) is None
        assert rp.contradiction({"lei_id": "B" * 20, "sec_cik": "0000000001"}, [], []) is None


class TestStale:
    """Q219635's CIK and LEI agree with the pre-2024 BlackRock, which EDGAR has
    called "BlackRock Finance, Inc." since 2024-09-26 (2026-10-29b rebuild)."""
    OLD_FILER = {"name": "BlackRock Finance, Inc.", "sec_cik": "0001364742",
                 "former_names": ["New BlackRock, Inc.|2006-09-29", "BlackRock, Inc.|2006-10-05",
                                  "BlackRock Inc.|2024-09-26"]}

    def test_an_item_that_names_the_node_only_by_an_ended_name_is_stale(self):
        why = rp.stale(self.OLD_FILER, ["BlackRock", "BlackRock, Inc.", "BLK"])
        assert why == "the item's name (blackrock) is a former name of BlackRock Finance, Inc."

    def test_on_a_day_before_the_rename_it_is_not(self):
        assert rp.stale(self.OLD_FILER, ["BlackRock"], on="2020-01-01") is None

    def test_a_name_the_node_still_bears_wins_over_an_ended_one(self):
        meta = {"name": "Meta Platforms, Inc.", "former_names": ["Facebook Inc|2021-10-27"]}
        assert rp.stale(meta, ["Meta Platforms", "Facebook"]) is None
        # a register's other name is a name in force too
        assert rp.stale({**self.OLD_FILER, "other_names": ["BlackRock"]}, ["BlackRock"]) is None

    def test_names_that_meet_nothing_and_a_node_without_former_names_say_nothing(self):
        assert rp.stale(self.OLD_FILER, ["Something Else"]) is None
        assert rp.stale({"name": "BlackRock Finance, Inc."}, ["BlackRock"]) is None
        assert rp.stale(self.OLD_FILER, [None, ""]) is None


class TestJoiningAFiler:
    """Before GLEIF's node is minted, an EDGAR filer node of its name may be
    the same legal entity (`legal_identity`, findings item 4): NYT's GLEIF
    node was minted beside its SEC node because the SEC node came first."""
    PROPS = {"name": "New York Times Co.", "other_names": [], "country": "US", "lei_id": "529900J1WEMMIW7BOH57",
             "hq_street": "620 Eighth Avenue", "address": None, "source_id": "gleif", "name_credibility": 92,
             "gleif_registration_status": "LAPSED", "gleif_last_update": "2021-07-16"}
    FILER = {"id": "sec:0000071691", "sec_cik": "0000071691", "lei_id": None, "country": "US",
             "other_names": None, "search_text": "NEW YORK TIMES CO"}

    def _run(self, verdict, rows=None, extra=(), by_name=True):
        from app.scraper.legal_identity import Filer
        sql, writes = [], []

        def run_sql(q, params=None):
            sql.append(params)
            return [dict(r) for r in (rows if rows is not None else [self.FILER])]
        with patch("app.scraper.maintenance._sec_lei_check_enabled", return_value=True), \
             patch("app.db.arcadedb.run_sql", side_effect=run_sql), \
             patch("app.db.arcadedb.run_command", side_effect=lambda q, p=None: writes.append((q, p))), \
             patch("app.scraper.legal_identity.filer_facts", return_value=Filer("0000071691", "NEW YORK TIMES CO")), \
             patch("app.scraper.legal_identity.same_legal_entity", return_value=verdict) as rule:
            got = rp._join_filer("529900J1WEMMIW7BOH57", dict(self.PROPS), list(extra), by_name)
        return got, sql, writes, rule

    def test_a_join_fills_the_filer_node_and_mints_nothing(self):
        got, sql, writes, rule = self._run(("join", "name+country+address@2021-07-16"))
        assert got == "sec:0000071691"
        assert sql[0] == {"nn": "new york times"}
        q, p = writes[0]
        assert p["lei"] == "529900J1WEMMIW7BOH57" and p["why"] == "name+country+address@2021-07-16"
        assert "e.hq_street = COALESCE(e.hq_street, $hq_street)" in q          # gaps only
        assert "e.name =" not in q and "e.source_id =" not in q                # the filer's own stay
        assert p["others"] == ["New York Times Co."] and "New York Times Co." in p["search"]
        assert rule.call_args.args[0]["lei_id"] == "529900J1WEMMIW7BOH57"

    def test_a_refusal_writes_nothing(self):
        got, _, writes, _ = self._run(("refuse", "registered US-MI, filer US-DE"))
        assert got is None and writes == []

    def test_a_filer_registered_in_another_country_is_not_asked(self):
        got, _, _, rule = self._run(("join", "x"), rows=[{**self.FILER, "country": "GB"}])
        assert got is None
        rule.assert_not_called()

    def test_only_the_given_node(self):
        got, sql, _, _ = self._run(("join", "name+place"), extra=["sec:0000071691"], by_name=False)
        assert got == "sec:0000071691" and sql == [{"id": "sec:0000071691"}]

    def test_sec_scraping_off_asks_nobody(self):
        with patch("app.scraper.maintenance._sec_lei_check_enabled", return_value=False), \
             patch("app.db.arcadedb.run_sql") as sql:
            assert rp._join_filer("X", dict(self.PROPS), []) is None
        sql.assert_not_called()


class TestAStatedLei:
    """A Wikidata item found by its CIK states an LEI the graph lacks: GLEIF is
    asked (findings item 3 — Baillie Gifford's 549300UYMK70HLNJU654)."""
    NODE = {"lei_id": None, "sec_cik": "0001088859", "name": "BAILLIE GIFFORD & CO"}

    def test_gleif_decides_on_the_resolved_node_only(self):
        with patch.object(rp, "_gleif_enabled", return_value=True), \
             patch("app.entity_resolution.resolve_entity_id", return_value=None), \
             patch("app.database.db.get_session"), \
             patch.object(rp, "write_from_gleif", return_value="sec:0001088859") as write:
            assert rp.attach_stated_lei(self.NODE, "sec:0001088859", ["549300UYMK70HLNJU654"]) == "sec:0001088859"
        write.assert_called_once_with("549300UYMK70HLNJU654", candidates=["sec:0001088859"], by_name=False)

    @pytest.mark.parametrize("node, held", [
        ({**NODE, "lei_id": "549300UYMK70HLNJU654"}, None),       # it has one
        ({**NODE, "sec_cik": None}, None),                        # no filer: the rule needs EDGAR
        (NODE, "lei:549300UYMK70HLNJU654"),                       # the graph holds the LEI
    ])
    def test_nothing_to_ask(self, node, held):
        with patch.object(rp, "_gleif_enabled", return_value=True), \
             patch("app.entity_resolution.resolve_entity_id", return_value=held), \
             patch("app.database.db.get_session"), \
             patch.object(rp, "write_from_gleif") as write:
            assert rp.attach_stated_lei(node, "sec:0001088859", ["549300UYMK70HLNJU654"]) is None
        write.assert_not_called()

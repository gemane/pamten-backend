"""The entity dedup's LEI bridge (`maintenance._bridge_sec_lei`): a same-name
group with an LEI-only node and a CIK-only EDGAR filer shares no hard id, so
the merge left it for review — Unilever's Companies House node and EDGAR's
UNILEVER PLC after the 2026-10-26 rebuild. Where the filer's own filings
state the LEI, the CIK node takes it and the ordinary merge folds the pair."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper import maintenance

LEI = "549300MKFYEKVRWML317"
URL = "https://www.sec.gov/Archives/edgar/data/217410/000021741023000039/a991-directorpdmrshareho.htm"


def members():
    return [{"id": "gb-coh:00041424", "name": "Unilever Plc", "lei_id": LEI, "sec_cik": None, "cred": 90},
            {"id": "sec:0000217410", "name": "UNILEVER PLC", "lei_id": None, "sec_cik": "0000217410", "cred": 98}]


@pytest.fixture(autouse=True)
def _no_edgar():
    # the registers' own word (legal_identity) asks EDGAR: never from a unit test
    with patch("app.scraper.legal_identity.filer_facts", return_value=None):
        yield


@pytest.fixture
def flags_on():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


class TestTheBridge:
    def test_a_confirmed_lei_goes_onto_the_filer_node(self):
        writes = []
        group = members()
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value={"source_url": URL}) as confirm, \
             patch.object(maintenance, "run_command", side_effect=lambda q, p=None: writes.append(p)):
            joined = maintenance._bridge_sec_lei(group, set())
        confirm.assert_called_once_with("0000217410", LEI, ["UNILEVER PLC", "Unilever Plc"])
        assert writes == [{"id": "sec:0000217410", "lei": LEI, "url": URL, "why": "filing"}]
        assert joined == [{"cik_id": "sec:0000217410", "lei_id": LEI, "lei_node_id": "gb-coh:00041424",
                           "name": "UNILEVER PLC", "source_url": URL}]
        assert maintenance._group_confidence(group) == "definitive"

    def test_unstated_the_registers_decide(self):
        # NYT states its LEI in no filing: name, place and the 2021 address agree
        from app.scraper.legal_identity import Filer
        writes = []
        nyt = [{"id": "lei:529900J1WEMMIW7BOH57", "name": "New York Times Co.", "lei_id": "529900J1WEMMIW7BOH57",
                "sec_cik": None, "cred": 92},
               {"id": "sec:0000071691", "name": "NEW YORK TIMES CO", "lei_id": None, "sec_cik": "0000071691",
                "cred": 98}]
        gleif = {"name": "New York Times Co.", "lei_id": "529900J1WEMMIW7BOH57", "country": "US"}
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None), \
             patch("app.scraper.legal_identity.filer_facts",
                   return_value=Filer("0000071691", "NEW YORK TIMES CO", home="US-NY")), \
             patch("app.scraper.legal_identity.same_legal_entity", return_value=("join", "name+country+address@2021-07-16")) as rule, \
             patch.object(maintenance, "run_sql", return_value=[gleif]), \
             patch.object(maintenance, "run_command", side_effect=lambda q, p=None: writes.append(p)):
            joined = maintenance._bridge_sec_lei(nyt, set())
        assert rule.call_args.args[0] == gleif
        assert writes == [{"id": "sec:0000071691", "lei": "529900J1WEMMIW7BOH57", "url": None,
                           "why": "name+country+address@2021-07-16"}]
        assert joined[0]["evidence"] == "name+country+address@2021-07-16"

    def test_unstated_and_refused_writes_nothing(self):
        from app.scraper.legal_identity import Filer
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None), \
             patch("app.scraper.legal_identity.filer_facts", return_value=Filer("0000217410", "UNILEVER PLC")), \
             patch("app.scraper.legal_identity.same_legal_entity", return_value=("refuse", "registered NL, filer GB")), \
             patch.object(maintenance, "run_sql", return_value=[{"name": "Unilever Plc"}]), \
             patch.object(maintenance, "run_command") as write:
            assert maintenance._bridge_sec_lei(members(), set()) == []
        write.assert_not_called()

    def test_not_confirmed_writes_nothing(self):
        group = members()
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None), \
             patch.object(maintenance, "run_command") as write:
            assert maintenance._bridge_sec_lei(group, set()) == []
        write.assert_not_called()
        assert maintenance._group_confidence(group) == "low"

    def test_a_pair_kept_apart_is_never_asked(self):
        with patch("app.scraper.sec_lei.confirm_filer_lei") as confirm:
            kept = {frozenset(("gb-coh:00041424", "sec:0000217410"))}
            assert maintenance._bridge_sec_lei(members(), kept) == []
        confirm.assert_not_called()

    def test_a_dry_run_writes_nothing_but_reports(self):
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value={"source_url": URL}), \
             patch.object(maintenance, "run_command") as write:
            assert len(maintenance._bridge_sec_lei(members(), set(), apply=False)) == 1
        write.assert_not_called()

    def test_nodes_holding_both_or_neither_id_are_no_candidates(self):
        group = [{"id": "a", "lei_id": LEI, "sec_cik": "0000217410"}, {"id": "b"}]
        with patch("app.scraper.sec_lei.confirm_filer_lei") as confirm:
            assert maintenance._bridge_sec_lei(group, set()) == []
        confirm.assert_not_called()

    def test_the_checks_per_group_are_capped(self):
        group = [{"id": f"l{i}", "lei_id": f"L{i}", "name": "x"} for i in range(4)] + \
                [{"id": f"c{i}", "sec_cik": f"{i}", "name": "x"} for i in range(4)]
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None) as confirm:
            maintenance._bridge_sec_lei(group, set())
        assert confirm.call_count == maintenance._SEC_LEI_PAIRS_PER_GROUP


class TestScopedDedup:
    def _run(self, confirmed):
        merges = []
        rows = {"SELECT name_normalized": [{"n": "unilever"}]}

        def sql(q, params=None):
            for k, v in rows.items():
                if q.startswith(k):
                    return v
            return members()
        with patch.object(maintenance, "run_sql", side_effect=sql), \
             patch.object(maintenance, "_not_duplicate_pairs", return_value=set()), \
             patch.object(maintenance, "run_command"), \
             patch("app.scraper.sec_lei.confirm_filer_lei", return_value=confirmed) as confirm, \
             patch.object(maintenance, "_apply_entity_merge", side_effect=lambda d, k: merges.append((d["id"], k["id"]))):
            out = maintenance.deduplicate_entities_for(["sec:0000217410"])
        return out, merges, confirm

    def test_a_confirmed_pair_merges(self, flags_on):
        out, merges, _ = self._run({"source_url": URL})
        assert merges == [("gb-coh:00041424", "sec:0000217410")]     # higher credibility survives
        assert out["entities_merged"] == 1 and len(out["lei_bridged"]) == 1

    def test_an_unconfirmed_pair_stays_for_review(self, flags_on):
        out, merges, _ = self._run(None)
        assert merges == [] and out["needs_review"] == 1

    def test_scraping_off_asks_nobody(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            out, merges, confirm = self._run({"source_url": URL})
        confirm.assert_not_called()
        assert merges == [] and out["needs_review"] == 1


class TestTheWholeDatabase:
    def test_each_filer_is_tried_against_its_name(self, flags_on):
        def sql(q, params=None):
            if "sec_cik IS NOT NULL AND lei_id IS NULL" in q:
                return [{"id": "sec:0000217410", "name": "UNILEVER PLC", "name_normalized": "unilever",
                         "sec_cik": "0000217410"},
                        {"id": "sec:0000320193", "name": "Apple Inc.", "name_normalized": "apple",
                         "sec_cik": "0000320193"}]
            if "CONTAINSTEXT" in q:
                return []                               # no partner by another name
            assert params == {"nn": "unilever"} or params == {"nn": "apple"}
            return [members()[0]] if params["nn"] == "unilever" else []
        with patch.object(maintenance, "run_sql", side_effect=sql), \
             patch.object(maintenance, "run_command"), \
             patch("app.scraper.sec_lei.confirm_filer_lei", return_value={"source_url": URL}) as confirm:
            joined = maintenance.bridge_sec_lei_pairs(set())
        confirm.assert_called_once()                   # Apple has no LEI-only namesake
        assert [j["cik_id"] for j in joined] == ["sec:0000217410"]

    def test_a_partner_by_its_other_name(self, flags_on):
        # Tencent's GLEIF legal name is Chinese; "Tencent Holdings Limited" is an other name
        def sql(q, params=None):
            if "sec_cik IS NOT NULL AND lei_id IS NULL" in q:
                return [{"id": "sec:0001293451", "name": "Tencent Holdings Ltd",
                         "name_normalized": "tencent holdings", "sec_cik": "0001293451"}]
            if "CONTAINSTEXT" in q:
                assert params == {"q": "tencent holdings"}
                return [{"id": "lei:254900N4SLUMW4XUYY11", "name": "騰訊控股有限公司", "lei_id": "254900N4SLUMW4XUYY11",
                         "sec_cik": None, "other_names": ["Tencent Holdings Limited"]},
                        {"id": "lei:OTHER", "name": "Tencent Music", "lei_id": "OTHER", "sec_cik": None,
                         "other_names": ["Tencent Music Entertainment Group"]}]
            return []
        seen = []
        with patch.object(maintenance, "run_sql", side_effect=sql), \
             patch.object(maintenance, "_bridge_sec_lei", side_effect=lambda m, k: seen.append(m) or []):
            maintenance.bridge_sec_lei_pairs(set())
        assert [m["id"] for m in seen[0]] == ["sec:0001293451", "lei:254900N4SLUMW4XUYY11"]

    def test_scraping_off_reads_nothing(self):
        with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False), \
             patch.object(maintenance, "run_sql") as sql:
            assert maintenance.bridge_sec_lei_pairs() == []
        sql.assert_not_called()

    def test_the_full_dedup_bridges_before_it_groups(self, flags_on):
        order = []
        with patch.object(maintenance, "_not_duplicate_pairs", return_value=set()), \
             patch.object(maintenance, "bridge_sec_lei_pairs", side_effect=lambda k: order.append("bridge") or []), \
             patch.object(maintenance, "_duplicate_keys", side_effect=lambda p: order.append(p) or []), \
             patch.object(maintenance, "_former_register_groups", return_value=[]):
            out = maintenance.deduplicate_entities(limit=None)
        assert order[0] == "bridge" and "lei_id" in order
        assert out["lei_bridged"] == []

"""What the registers would correct on Wikidata (2026-10-29) — a report,
never an edit. The cases are the dev findings of that day: BlackRock's stale
LEI, J.P. Morgan Securities' LEI on the UK PLC's item, Google's two CIKs, the
Vanguard Group's missing LEI, and Tencent's node carrying Tencent Music's CIK.
"""
import csv
import json
import pathlib
from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.scraper import register_pointer as rp
from app.scraper import wikidata_suggestions as ws

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "gleif_api_blackrock_finance.json"
US = {"Q30": "US"}


def _stmt(prop, value, rank="normal"):
    dv = {"value": {"id": value}} if prop == "P17" else {"value": value}
    return {"mainsnak": {"property": prop, "datavalue": dv}, "rank": rank}


def _item(label="The Vanguard Group", aliases=(), countries=("Q30",), leis=(), ciks=()):
    return {"label": label, "aliases": list(aliases), "country_qids": list(countries),
            "P1278": list(leis), "P5531": list(ciks)}


def _node(**kw):
    return {"id": "n1", "name": "THE VANGUARD GROUP, INC.", "wikidata_id": "Q849363",
            "lei_id": None, "sec_cik": None, **kw}


def _registers(table):
    """register(prop, value) over a dict; a value not in it is unknown."""
    return lambda prop, value: table.get((prop, value))


VANGUARD_LEI = "5493002789CX3L0CJP65"
VANGUARD = {"names": ["THE VANGUARD GROUP, INC."], "country": "US", "status": "ISSUED"}


class TestTheStatementsAnItemMakes:
    def test_deprecated_values_are_left_out_and_ciks_padded(self):
        claims = {"P5531": [_stmt("P5531", "1364742"), _stmt("P5531", "0002012383", "deprecated")],
                  "P1278": [_stmt("P1278", "549300lrif3nwcu26a80"), _stmt("P1278", "549300LRIF3NWCU26A80")]}
        assert ws._statements(claims, "P5531") == ["0001364742"]
        assert ws._statements(claims, "P1278") == ["549300LRIF3NWCU26A80"]   # one value, normalised once

    def test_label_aliases_and_countries(self):
        facts = ws._item_facts({"labels": {"en": {"value": "Unilever"}},
                                "aliases": {"en": [{"value": "Unilever PLC"}]},
                                "claims": {"P17": [_stmt("P17", "Q145"), _stmt("P17", "Q55", "deprecated")]}})
        assert facts["label"] == "Unilever" and facts["aliases"] == ["Unilever PLC"]
        assert facts["country_qids"] == ["Q145"] and facts["P1278"] == [] and facts["P5531"] == []


class TestMissing:
    def test_name_and_country_make_a_confident_addition(self):
        rows = ws.suggestions_for(_node(lei_id=VANGUARD_LEI), _item(),
                                  _registers({("P1278", VANGUARD_LEI): VANGUARD}), US)
        assert len(rows) == 1
        r = rows[0]
        assert (r["kind"], r["action"], r["confidence"]) == ("missing", "add", "high")
        assert r["property"] == "P1278" and r["suggested"] == VANGUARD_LEI
        assert r["evidence_url"] == f"https://search.gleif.org/#/record/{VANGUARD_LEI}"
        assert r["register_name"] == "THE VANGUARD GROUP, INC." and r["qid"] == "Q849363"

    def test_name_and_the_items_other_id_make_a_confident_addition(self):
        # Embraer: EDGAR states no country for it, but the item's LEI is the node's
        node = _node(name="EMBRAER S.A.", wikidata_id="Q105557", lei_id="549300EMBRAER0000001", sec_cik="1355444")
        regs = _registers({("P5531", "0001355444"): {"names": ["EMBRAER S.A."], "country": None},
                           ("P1278", "549300EMBRAER0000001"): {"names": ["EMBRAER S.A."], "country": "BR"}})
        rows = ws.suggestions_for(node, _item(label="Embraer", countries=("Q155",), leis=["549300EMBRAER0000001"]), regs, {})
        assert [(r["property"], r["confidence"]) for r in rows] == [("P5531", "high")]
        assert rows[0]["suggested"] == "0001355444"
        assert rows[0]["evidence_url"].endswith("CIK=0001355444")

    def test_a_name_alone_is_not_enough(self):
        # the node may have met the item by name only — how the BlackRock mix-up began
        rows = ws.suggestions_for(_node(lei_id=VANGUARD_LEI), _item(countries=()),
                                  _registers({("P1278", VANGUARD_LEI): VANGUARD}), US)
        assert rows[0]["confidence"] == "review" and rows[0]["note"] == "agrees on name"

    def test_another_register_name_is_not_a_match_even_with_the_other_id(self):
        # Tencent's node holds Tencent Holdings' LEI and Tencent Music's CIK
        node = _node(name="Tencent", wikidata_id="Q860580", lei_id="5493TENCENTHOLDING01", sec_cik="1744676")
        regs = _registers({("P5531", "0001744676"): {"names": ["Tencent Music Entertainment Group"], "country": "KY"},
                           ("P1278", "5493TENCENTHOLDING01"): {"names": ["Tencent Holdings Limited"], "country": "KY"}})
        rows = ws.suggestions_for(node, _item(label="Tencent", countries=("Q148",), leis=["5493TENCENTHOLDING01"]), regs, {"Q148": "CN"})
        assert [(r["property"], r["confidence"]) for r in rows] == [("P5531", "review")]
        assert rows[0]["register_name"] == "Tencent Music Entertainment Group"

    def test_an_alias_matches(self):
        node = _node(name="FMR LLC", sec_cik="315066")
        regs = _registers({("P5531", "0000315066"): {"names": ["FMR LLC"], "country": "US"}})
        rows = ws.suggestions_for(node, _item(label="Fidelity Investments", aliases=["FMR LLC"]), regs, US)
        assert rows[0]["confidence"] == "high"

    def test_a_former_name_matches(self):
        node = _node(sec_cik="1")
        regs = _registers({("P5531", "0000000001"): {"names": ["NEW NAME INC", "THE VANGUARD GROUP INC"], "country": "US"}})
        assert ws.suggestions_for(node, _item(), regs, US)[0]["confidence"] == "high"

    def test_a_node_id_its_register_does_not_know_says_nothing(self):
        assert ws.suggestions_for(_node(lei_id=VANGUARD_LEI), _item(), _registers({}), US) == []

    def test_a_node_without_register_ids_says_nothing(self):
        assert ws.suggestions_for(_node(), _item(), _registers({}), US) == []


class TestDiffers:
    def test_blackrock_names_whose_the_stated_lei_is(self):
        node = _node(id="lei:529900VBK42Y5HHRMD23", name="BlackRock, Inc.", wikidata_id="Q219635",
                     lei_id="529900VBK42Y5HHRMD23")
        regs = _registers({("P1278", "529900VBK42Y5HHRMD23"): {"names": ["BlackRock, Inc."], "country": "US"},
                           ("P1278", "549300LRIF3NWCU26A80"): {"names": ["BlackRock Finance, Inc."], "country": "US",
                                                               "status": "ISSUED"}})
        rows = ws.suggestions_for(node, _item(label="BlackRock", leis=["549300LRIF3NWCU26A80"]), regs, US)
        assert len(rows) == 1
        r = rows[0]
        assert (r["kind"], r["action"], r["confidence"]) == ("differs", "review", "review")
        assert r["current"] == "549300LRIF3NWCU26A80" and r["suggested"] == "529900VBK42Y5HHRMD23"
        assert r["note"] == "549300LRIF3NWCU26A80 is BlackRock Finance, Inc."

    def test_a_lapsed_stated_id_says_so(self):
        node = _node(lei_id="5493NEW0000000000001")
        regs = _registers({("P1278", "5493NEW0000000000001"): VANGUARD,
                           ("P1278", "5493OLD0000000000001"): {"names": ["OLD CO"], "country": "US", "status": "LAPSED"}})
        rows = ws.suggestions_for(node, _item(leis=["5493OLD0000000000001"]), regs, US)
        assert rows[0]["note"] == "5493OLD0000000000001 is OLD CO (LAPSED)"

    def test_differs_never_goes_into_the_batch(self):
        node = _node(lei_id="5493NEW0000000000001")
        regs = _registers({("P1278", "5493NEW0000000000001"): VANGUARD, ("P1278", "5493OLD0000000000001"): VANGUARD})
        rows = ws.suggestions_for(node, _item(leis=["5493OLD0000000000001"]), regs, US)
        assert [r["kind"] for r in rows] == ["differs"]
        assert ws.quickstatements(rows, date(2026, 10, 29)) == []


class TestUnknownAndSeveral:
    def test_a_stated_id_no_register_knows(self):
        rows = ws.suggestions_for(_node(), _item(ciks=["0000000099"]), _registers({}), US)
        assert [(r["kind"], r["note"]) for r in rows] == [("unknown", "EDGAR does not know 0000000099")]

    def test_googles_two_ciks_are_information(self):
        node = _node(name="Google LLC", wikidata_id="Q95", sec_cik="1824723")
        regs = _registers({("P5531", "0001824723"): {"names": ["Google LLC"], "country": "US"},
                           ("P5531", "0001288776"): {"names": ["GOOGLE INC."], "country": "US"}})
        rows = ws.suggestions_for(node, _item(label="Google", ciks=["0001824723", "0001288776"]), regs, US)
        assert [(r["kind"], r["action"]) for r in rows] == [("several", "info")]

    def test_one_stated_value_that_is_the_nodes_says_nothing(self):
        node = _node(lei_id=VANGUARD_LEI)
        rows = ws.suggestions_for(node, _item(leis=[VANGUARD_LEI]),
                                  _registers({("P1278", VANGUARD_LEI): VANGUARD}), US)
        assert rows == []


class TestRegisterFacts:
    def test_gleif_through_the_pointers_mapping(self):
        rec = rp.golden_copy_record(json.loads(FIXTURE.read_text())["data"]["attributes"])
        with patch("app.scraper.register_pointer.fetch_gleif_record", return_value=rec):
            facts = ws.gleif_facts("549300LRIF3NWCU26A80")
        assert facts["names"] == ["BlackRock Finance, Inc.", "BLACKROCK, INC."]
        assert facts["country"] == "US" and facts["status"] == "ISSUED"

    def test_gleif_unknown(self):
        with patch("app.scraper.register_pointer.fetch_gleif_record", return_value=None):
            assert ws.gleif_facts("X") is None

    def test_edgar_names_and_country(self):
        sub = {"name": "BlackRock Finance, Inc.", "formerNames": [{"name": "BLACKROCK INC."}],
               "stateOfIncorporation": "DE"}
        with patch("app.scraper.sec_edgar._submissions", return_value=sub):
            facts = ws.edgar_facts("1364742")
        assert facts["names"] == ["BlackRock Finance, Inc.", "BLACKROCK INC."] and facts["country"] == "US"

    def test_edgar_404_is_unknown_and_other_errors_raise(self):
        def err(code):
            resp = httpx.Response(code, request=httpx.Request("GET", "https://data.sec.gov/x"))
            return httpx.HTTPStatusError("e", request=resp.request, response=resp)
        with patch("app.scraper.sec_edgar._submissions", side_effect=err(404)):
            assert ws.edgar_facts("1") is None
        with patch("app.scraper.sec_edgar._submissions", side_effect=err(500)):
            with pytest.raises(httpx.HTTPStatusError):
                ws.edgar_facts("1")


def _api(body):
    r = MagicMock()
    r.json.return_value = body
    return r


class TestReadingWikidata:
    def test_items_in_batches_of_50_and_missing_items_dropped(self):
        qids = [f"Q{i}" for i in range(1, 52)]
        calls = []

        def fake(url, params, timeout):
            ids = params["ids"].split("|")
            calls.append(len(ids))
            ents = {q: {"labels": {"en": {"value": q}}, "claims": {}} for q in ids}
            if "Q51" in ents:
                ents["Q51"] = {"id": "Q51", "missing": ""}
            return _api({"entities": ents})
        with patch("app.scraper.wikidata._wd_get", side_effect=fake):
            items = ws.fetch_items(qids)
        assert calls == [50, 1] and len(items) == 50 and "Q51" not in items

    def test_an_api_error_raises(self):
        from app.scraper.wikidata import WikidataApiError
        with patch("app.scraper.wikidata._wd_get", return_value=_api({"error": {"code": "maxlag", "info": "x"}})):
            with pytest.raises(WikidataApiError):
                ws.fetch_items(["Q1"])

    def test_country_codes(self):
        body = {"entities": {"Q30": {"claims": {"P297": [_stmt("P297", "us")]}},
                             "Q145": {"claims": {"P297": [_stmt("P297", "XX", "deprecated"), _stmt("P297", "GB")]}}}}
        with patch("app.scraper.wikidata._wd_get", return_value=_api(body)):
            assert ws.fetch_country_codes({"Q30", "Q145"}) == {"Q30": "US", "Q145": "GB"}


class TestItemsStatingAnId:
    def _sparql(self, bindings):
        return _api({"results": {"bindings": bindings}})

    def test_ciks_asked_padded_and_bare_and_read_back_padded(self):
        seen = []

        def fake(url, params, timeout):
            seen.append(params["query"])
            return self._sparql([
                {"item": {"value": "http://www.wikidata.org/entity/Q219635"}, "v": {"value": "1364742"}},
                {"item": {"value": "http://www.wikidata.org/entity/Q219635"}, "v": {"value": "0001364742"}},
                {"item": {"value": "http://www.wikidata.org/entity/Q2"}, "v": {"value": "0001364742"}}])
        with patch("app.scraper.wikidata._wd_get", side_effect=fake):
            found = ws.items_stating("P5531", ["0001364742"])
        assert found == {"0001364742": ["Q219635", "Q2"]}
        q = seen[0]
        assert '"0001364742"' in q and '"1364742"' in q
        assert "p:P5531" in q and "wikibase:DeprecatedRank" in q

    def test_leis_in_batches(self):
        leis = [f"{i:020d}" for i in range(ws._SPARQL_BATCH + 1)]
        sizes = []

        def fake(url, params, timeout):
            sizes.append(params["query"].count('"'))
            return self._sparql([])
        with patch("app.scraper.wikidata._wd_get", side_effect=fake):
            assert ws.items_stating("P1278", leis) == {}
        assert sizes == [2 * ws._SPARQL_BATCH, 2]


BLACKROCK_FINANCE = {"names": ["BlackRock Finance, Inc."], "country": "US", "status": "ISSUED"}
BLACKROCK_INC = {"names": ["BlackRock, Inc."], "country": "US", "status": "ISSUED"}
OLD_LEI, NEW_LEI = "549300LRIF3NWCU26A80", "529900VBK42Y5HHRMD23"


class TestRefused:
    """BlackRock after the 2026-10-29 rebuild: the scraper refused Q219635 for
    lei:529900VBK… and wrote no item id, so the linked report never saw it."""

    def test_the_contradiction_is_tagged_and_named(self):
        node = _node(id="lei:" + NEW_LEI, name="BlackRock Finance, Inc.", wikidata_id="Q219635",
                     lei_id=NEW_LEI, sec_cik="0001364742")
        item = _item(label="BlackRock", leis=[OLD_LEI], ciks=["0001364742"])
        rows = ws.refused_rows(node, item, f"LEI {NEW_LEI} is not the stated {OLD_LEI}",
                               _registers({("P1278", NEW_LEI): BLACKROCK_INC,
                                           ("P1278", OLD_LEI): BLACKROCK_FINANCE}), US)
        assert [(r["kind"], r["property"], r["link"]) for r in rows] == [("differs", "P1278", "refused")]
        assert rows[0]["note"] == (f"refused: LEI {NEW_LEI} is not the stated {OLD_LEI}; "
                                   f"{OLD_LEI} is BlackRock Finance, Inc.")

    def test_a_confident_addition_is_never_confident_on_a_refused_item(self):
        node = _node(lei_id=VANGUARD_LEI, sec_cik="0000102909")
        item = _item(ciks=["0000000001"])          # a name and country match, but another CIK
        regs = _registers({("P1278", VANGUARD_LEI): VANGUARD, ("P5531", "0000000001"): VANGUARD})
        assert any(r["confidence"] == "high" for r in ws.suggestions_for(node, item, regs, US))
        rows = ws.refused_rows(node, item, "CIK 0000102909 is not the stated 0000000001", regs, US)
        assert rows and all(r["confidence"] != "high" and r["link"] == "refused" for r in rows)
        assert ws.quickstatements(rows, date(2026, 10, 29)) == []

    def test_a_refused_row_when_nothing_else_is_said(self):
        node = _node(lei_id=NEW_LEI)               # GLEIF does not know the node's LEI here
        rows = ws.refused_rows(node, _item(leis=[OLD_LEI]), f"LEI {NEW_LEI} is not the stated {OLD_LEI}",
                               _registers({("P1278", OLD_LEI): BLACKROCK_FINANCE}), US)
        assert [(r["kind"], r["suggested"], r["current"]) for r in rows] == [("refused", NEW_LEI, OLD_LEI)]
        assert rows[0]["note"].startswith("refused: LEI")


class TestTheReport:
    def _build(self, nodes, items, iso, gleif, edgar, unlinked=(), stating=None):
        stating = stating or {}
        with patch.object(ws, "graph_nodes", return_value=nodes), \
             patch.object(ws, "unlinked_nodes", return_value=list(unlinked)), \
             patch.object(ws, "items_stating", side_effect=lambda prop, values: {
                 v: q for v, q in stating.get(prop, {}).items() if v in values}), \
             patch.object(ws, "fetch_items", return_value=items), \
             patch.object(ws, "fetch_country_codes", return_value=iso), \
             patch.object(ws, "gleif_facts", side_effect=gleif) as g, \
             patch.object(ws, "edgar_facts", side_effect=edgar):
            return ws.build_report(), g

    def test_counts_and_one_fetch_per_register_record(self):
        nodes = [_node(id="a", wikidata_id="Q1", lei_id=VANGUARD_LEI),
                 _node(id="b", wikidata_id="Q2", lei_id=VANGUARD_LEI),
                 _node(id="c", wikidata_id="Q3")]                      # its item is gone
        items = {"Q1": _item(), "Q2": _item(countries=())}
        report, g = self._build(nodes, items, US, lambda lei: VANGUARD, lambda cik: None)
        assert g.call_count == 1
        assert report["counts"] == {"nodes": 3, "items": 2, "refused_pairs": 0,
                                    "missing_high": 1, "missing": 1}
        assert {r["link"] for r in report["rows"]} == {"graph"}

    def test_a_refused_pair_is_reported_and_an_agreeing_one_is_not(self):
        unlinked = [{"id": "lei:" + NEW_LEI, "name": "BlackRock Finance, Inc.",
                     "lei_id": NEW_LEI, "sec_cik": "0001364742"},
                    {"id": "agree", "name": "THE VANGUARD GROUP, INC.", "lei_id": VANGUARD_LEI, "sec_cik": None}]
        items = {"Q219635": _item(label="BlackRock", leis=[OLD_LEI], ciks=["0001364742"]),
                 "Q849363": _item(leis=[VANGUARD_LEI])}
        stating = {"P5531": {"0001364742": ["Q219635"]}, "P1278": {VANGUARD_LEI: ["Q849363"]}}
        regs = {NEW_LEI: BLACKROCK_INC, OLD_LEI: BLACKROCK_FINANCE, VANGUARD_LEI: VANGUARD}
        report, _ = self._build([], items, US, regs.get, lambda cik: None, unlinked, stating)
        assert report["counts"]["refused_pairs"] == 1
        assert [(r["qid"], r["node_id"], r["kind"], r["link"]) for r in report["rows"]] == [
            ("Q219635", "lei:" + NEW_LEI, "differs", "refused")]

    def test_an_item_that_names_its_ids_only_by_an_ended_name_is_refused(self):
        """Q219635's ids agree with the pre-2024 filer — "BlackRock Finance, Inc."
        since 2024-09-26 — and the item calls it "BlackRock": refused as stale,
        whether the node holds the item already (dev before the rule) or not."""
        old_filer = {"id": "sec:0001364742", "name": "BlackRock Finance, Inc.", "lei_id": None,
                     "sec_cik": "0001364742", "former_names": ["BlackRock Inc.|2024-09-26"]}
        items = {"Q219635": _item(label="BlackRock", aliases=["BlackRock, Inc."], ciks=["0001364742"])}
        edgar = {"0001364742": {"names": ["BlackRock Finance, Inc."], "country": "US", "status": None}}
        report, _ = self._build([], items, US, lambda lei: None, edgar.get, [old_filer],
                                {"P5531": {"0001364742": ["Q219635"]}})
        assert report["counts"]["refused_pairs"] == 1
        [row] = report["rows"]
        assert (row["qid"], row["kind"], row["property"], row["link"]) == ("Q219635", "refused", "P5531", "refused")
        assert row["note"] == "refused: the item's name (blackrock) is a former name of BlackRock Finance, Inc."
        linked, _ = self._build([{**old_filer, "wikidata_id": "Q219635"}], items, US,
                                lambda lei: None, edgar.get)
        assert linked["counts"]["refused_pairs"] == 1
        assert [(r["kind"], r["link"]) for r in linked["rows"]] == [("refused", "refused")]

    def test_the_files(self, tmp_path):
        rows = ws.suggestions_for(_node(lei_id=VANGUARD_LEI), _item(),
                                  _registers({("P1278", VANGUARD_LEI): VANGUARD}), US)
        rows += ws.suggestions_for(_node(lei_id=VANGUARD_LEI), _item(countries=()),
                                   _registers({("P1278", VANGUARD_LEI): VANGUARD}), US)
        paths = ws.write_report({"rows": rows, "counts": {}}, str(tmp_path / "out"), date(2026, 10, 29))
        assert paths["statements"] == 1
        with open(paths["csv"], newline="") as f:
            read = list(csv.DictReader(f))
        assert [r["confidence"] for r in read] == ["high", "review"]
        assert list(read[0]) == list(ws.CSV_FIELDS)
        qs = pathlib.Path(paths["quickstatements"]).read_text()
        assert qs == (f'Q849363\tP1278\t"{VANGUARD_LEI}"\tS854\t'
                      f'"https://search.gleif.org/#/record/{VANGUARD_LEI}"\tS813\t+2026-10-29T00:00:00Z/11\n')
        assert paths["csv"].endswith("wikidata-suggestions-2026-10-29.csv")

    def test_an_empty_batch_is_an_empty_file(self, tmp_path):
        paths = ws.write_report({"rows": [], "counts": {}}, str(tmp_path), date(2026, 10, 29))
        assert pathlib.Path(paths["quickstatements"]).read_text() == "" and paths["statements"] == 0

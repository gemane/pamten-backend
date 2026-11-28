"""The daily probes of the outside sources (app/source_contracts.py) — the rules,
over stubbed scraper functions; nothing here reaches a source."""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import source_calls, source_contracts as sc
from app.config import settings
from app.scraper import ch_officers, legal_identity, sec_13dg, sec_client, sec_lookup, sec_roster, wikidata

APPLE = sc.APPLE_CIK


@pytest.fixture(autouse=True)
def no_pause():
    """The insider-page probe waits between its asks; no test does."""
    with patch.object(sc.time, "sleep") as sleep:
        yield sleep


def _status_error(status, text=""):
    resp = httpx.Response(status, text=text, request=httpx.Request("GET", "https://www.sec.gov/x"))
    return httpx.HTTPStatusError(f"HTTP {status}", request=resp.request, response=resp)


class TestRun:
    def _probe(self, *outcomes):
        answers = list(outcomes)

        def run():
            a = answers.pop(0)
            if isinstance(a, BaseException):
                raise a
        return sc.Probe("sec-edgar", "a question", run), answers

    def test_a_probe_that_answers_is_not_reported(self):
        probe, _ = self._probe(None)
        assert sc.run([probe], sleep=lambda s: None) == []

    def test_a_probe_that_fails_once_is_asked_again_after_a_pause(self):
        probe, left = self._probe(sc.Broken("slow today"), None)
        pauses = []
        assert sc.run([probe], sleep=pauses.append) == []
        assert pauses == [sc.RETRY_PAUSE] and left == []

    def test_a_probe_that_fails_twice_is_reported_with_the_second_reason(self):
        probe, _ = self._probe(sc.Broken("first"), sc.Broken("lists nobody"))
        assert sc.run([probe], sleep=lambda s: None) == [(probe, "lists nobody")]

    def test_a_refusal_carries_the_sources_own_words(self):
        err = _status_error(400, "Invalid count,\n Received: 5")
        probe, _ = self._probe(err, err)
        [(_, why)] = sc.run([probe], sleep=lambda s: None)
        assert why == "HTTP 400: Invalid count, Received: 5"

    def test_a_page_for_an_answer_is_reported_by_its_words(self):
        # findings item 153: SEC.gov's maintenance page behind the insider page's 503
        page = ("<!DOCTYPE html> <html lang=\"en\"><head><title>SEC.gov | File Unavailable</title>"
                "<style>p { color: red }</style></head><body><!-- More Search Options -->"
                "<p>This page is temporarily unavailable.</p><script>var a = 1;</script></body></html>")
        probe = sc.Probe("sec-edgar", "x", MagicMock(side_effect=_status_error(503, page)))
        [(_, why)] = sc.run([probe], sleep=lambda s: None)
        assert why == "HTTP 503: This page is temporarily unavailable."

    def test_any_other_error_is_named(self):
        probe, _ = self._probe(httpx.ConnectError("no route"), httpx.ConnectError("no route"))
        [(_, why)] = sc.run([probe], sleep=lambda s: None)
        assert why == "ConnectError: no route"

    def test_one_failing_probe_does_not_stop_the_others(self):
        bad, _ = self._probe(sc.Broken("a"), sc.Broken("a"))
        good, left = self._probe(None)
        assert [p for p, _ in sc.run([bad, good], sleep=lambda s: None)] == [bad] and left == []


# what each probe reads: (probe function, the scraper function it calls, a good
# answer, answers that must break it)
_OFFICER = {"name": "SMITH, John", "links": {"officer": {"appointments": "/officers/x/appointments"}}}
CASES = [
    (sc._sec_tickers, (sec_lookup, "_get_tickers"),
     {"0": {"cik_str": 320193}}, [{}, {"0": {"cik_str": 1}}, None]),
    (sc._sec_company_search, (sec_lookup, "_browse_index"),
     [(APPLE, None)], [None, [], [("0000000001", "Other Inc")]]),
    (sc._sec_person_search, (sec_lookup, "_search_person_ciks"),
     ["0000000009", sc.COOK_CIK], [[], ["0000000009"]]),
    (sc._sec_submissions, (sec_client, "_submissions"),
     {"filings": {"recent": {"form": ["10-K"], "accessionNumber": ["a"], "primaryDocument": ["d"]}}},
     [{}, {"filings": {"recent": {"form": []}}}, {"filings": {"recent": {"form": ["10-K"]}}}]),
    (sc._sec_holder_feed, (sec_13dg, "_ownership_feed_entries"),
     iter([{"accession": "a", "index_url": "u"}]), [iter([]), iter([{"accession": "a"}])]),
    (sc._sec_insider_page, (sec_roster, "issuer_owners"),
     [{"name": "LEVINSON ARTHUR D", "cik": "0001214128"}, {"name": "COOK TIMOTHY D", "cik": sc.COOK_CIK}],
     [[], [{"name": "LEVINSON ARTHUR D", "cik": "0001214128"}]]),
    (sc._sec_full_text, (sec_client, "_get"),
     {"hits": {"hits": [{"_source": {"ciks": ["320193"]}}]}},
     [{}, {"hits": {"hits": []}}, {"hits": {"hits": [{"_source": {}}]}}]),
    (sc._sec_statement_index, (sec_client, "_get"),
     {"hits": {"hits": [{"_source": {"form": "SC 13G"}}, {"_source": {"form": "SCHEDULE 13G/A"}}]}},
     [{}, {"hits": {"hits": []}},
      {"hits": {"hits": [{"_source": {"form": "SC 13G/A"}}, {"_source": {"form": "SCHEDULE 13G/A"}}]}},   # item 164
      {"hits": {"hits": [{"_source": {"form": "SC 13G"}}]}}]),
    (sc._gleif_relationship, (legal_identity, "_gleif_get"),
     None, [{"errors": []}]),
    (sc._wikidata_by_statement, (wikidata, "search_by_statement"),
     [{"id": "Q312"}], [[], [{"id": "Q1"}]]),
    (sc._wikidata_by_name, (wikidata, "search_entity"),
     [{"id": "Q9"}, {"id": "Q312"}], [[], [{"id": "Q9"}]]),
    (sc._wikidata_entity, (wikidata, "_names_for"),
     {"Q312": ["Apple Inc."]}, [{}, {"Q312": []}]),
    (sc._companies_house_officers, (ch_officers, "fetch_officers"),
     [_OFFICER], [None, [], [{"name": "SMITH, John"}], [{"links": _OFFICER["links"]}]]),
]


class TestWhatEachProbeReads:
    @pytest.mark.parametrize("probe, target, good, bad", CASES, ids=lambda v: getattr(v, "__name__", None))
    def test_the_expected_answer_passes(self, probe, target, good, bad):
        with patch.object(*target, return_value=good), patch.object(ch_officers, "client", MagicMock()):
            probe()

    @pytest.mark.parametrize("probe, target, answer",
                             [(p, t, a) for p, t, _, bad in CASES for a in bad],
                             ids=lambda v: getattr(v, "__name__", None))
    def test_another_answer_breaks_it(self, probe, target, answer):
        with patch.object(*target, return_value=answer), patch.object(ch_officers, "client", MagicMock()), \
             pytest.raises(sc.Broken):
            probe()

    def test_the_gleif_relationship_may_be_a_record(self):
        with patch.object(legal_identity, "_gleif_get", return_value={"data": {}}):
            sc._gleif_relationship()

    def test_the_gleif_record_needs_a_name_and_an_address(self):
        def answer(entity, status=200):
            return httpx.Response(status, json={"data": {"attributes": {"entity": entity}}},
                                  request=httpx.Request("GET", "https://api.gleif.org/x"))
        good = {"legalName": {"name": "Apple Inc."}, "legalAddress": {"country": "US"}}
        with patch("httpx.get", return_value=answer(good)) as get:
            sc._gleif_record()
        assert get.call_args.args[0] == f"https://api.gleif.org/api/v1/lei-records/{sc.APPLE_LEI}"
        for entity in ({}, {"legalName": {"name": "Apple Inc."}}, {"legalAddress": {"country": "US"}}):
            with patch("httpx.get", return_value=answer(entity)), pytest.raises(sc.Broken):
                sc._gleif_record()
        with patch("httpx.get", return_value=answer(good, 400)), pytest.raises(httpx.HTTPStatusError):
            sc._gleif_record()


class TestTheProbeList:
    def test_every_probe_belongs_to_a_counted_source(self):
        # the refused alert and the probes name a source the same way
        assert {p.source for p in sc.probes()} <= set(source_calls.SOURCE_HOSTS.values())

    def test_companies_house_is_asked_only_with_its_key(self):
        with patch.object(settings, "COMPANIES_HOUSE_API_KEY", ""):
            assert "companies-house" not in {p.source for p in sc.probes()}
        with patch.object(settings, "COMPANIES_HOUSE_API_KEY", "k"):
            assert "companies-house" in {p.source for p in sc.probes()}

    def test_the_bulk_name_list_is_asked_only_where_one_is_kept(self):
        assert "bulk name list" not in {p.name for p in sc.probes()}          # the suite keeps none
        with patch.object(settings, "SEC_NAME_LIST_DIR", "/somewhere"):
            assert "bulk name list" in {p.name for p in sc.probes()}

    def test_the_bulk_name_list_must_hold_a_known_filer(self):
        from app.scraper import sec_names
        with patch.object(sec_names, "index_path", return_value="idx"):
            with patch.object(sec_names, "person_ciks", return_value=["0000000009", sc.COOK_CIK]):
                sc._sec_name_list()
            with patch.object(sec_names, "person_ciks", return_value=[]), pytest.raises(sc.Broken):
                sc._sec_name_list()
        with patch.object(sec_names, "index_path", return_value=None), pytest.raises(sc.Broken):
            sc._sec_name_list()                                               # no download and none at hand

    def test_the_person_search_probe_asks_the_live_search_whatever_list_is_kept(self):
        from app.scraper import sec_names
        with patch.object(sec_names, "person_ciks", return_value=[sc.COOK_CIK]) as listed, \
             patch.object(sec_lookup, "_search_person_ciks", return_value=[]), pytest.raises(sc.Broken):
            sc._sec_person_search()
        listed.assert_not_called()

    def test_every_edgar_request_type_has_a_probe_or_a_reason(self):
        # findings item 153: the insider page answered 503 for a day and no probe asked it
        with patch.object(settings, "SEC_NAME_LIST_DIR", "/somewhere"):
            probed = {p.asks for p in sc.probes() if p.source == "sec-edgar"}
        assert "" not in probed, "an EDGAR probe that names no request type"
        assert "insider-page" in probed
        assert probed | set(sc.SEC_NOT_PROBED) == set(source_calls.SEC_TYPES)
        assert not probed & set(sc.SEC_NOT_PROBED)

    def test_the_insider_page_probe_goes_through_the_roster_s_own_reader(self):
        page = ("<table><tr><th>Owner</th><th>Filings</th><th>Transaction Date</th><th>Type of Owner</th></tr>"
                f"<tr><td>COOK TIMOTHY D</td><td>{int(sc.COOK_CIK)}</td><td>2026-10-01</td><td>officer: CEO</td></tr></table>")
        with patch.object(sec_client, "_get_text", return_value=page) as get:
            sc._sec_insider_page()
        assert get.call_args.args == (sec_roster.OWNERS_URL, {"action": "getissuer", "CIK": sc.APPLE_CIK})
        assert source_calls.request_type("sec-edgar", httpx.URL(sec_roster.OWNERS_URL, params=get.call_args.args[1])) \
            == "insider-page"
        # SEC.gov's maintenance page: a page, and no owner table in it
        with patch.object(sec_client, "_get_text", return_value="<html>This page is temporarily unavailable.</html>"), \
             pytest.raises(sc.Broken, match="0 owners read"):
            sc._sec_insider_page()

    def test_the_insider_page_must_answer_every_time_it_is_asked(self, no_pause):
        # 2026-11-24: one request in four got through, and so did a probe that asked once
        cook = [{"name": "COOK TIMOTHY D", "cik": sc.COOK_CIK}]
        with patch.object(sec_roster, "issuer_owners", return_value=cook) as asked:
            sc._sec_insider_page()
        assert asked.call_count == sc.INSIDER_PAGE_ASKS == 5
        assert no_pause.call_args_list == [((sc.INSIDER_PAGE_PAUSE,),)] * 4      # between the asks, not before
        answers = [cook, cook, cook, cook, _status_error(503)]
        with patch.object(sec_roster, "issuer_owners", side_effect=answers), pytest.raises(httpx.HTTPStatusError):
            sc._sec_insider_page()

    def test_a_refused_insider_page_is_reported_with_its_status(self):
        probe = sc.Probe("sec-edgar", "a company's reporting owners", sc._sec(sc._sec_insider_page), "insider-page")
        with patch.object(sec_client, "_get_text", side_effect=_status_error(503)):
            [(_, why)] = sc.run([probe], sleep=lambda s: None)
        assert "503" in why

    def test_every_source_with_a_scraper_has_a_probe(self):
        with patch.object(settings, "COMPANIES_HOUSE_API_KEY", "k"):
            assert {"sec-edgar", "gleif", "wikidata", "companies-house"} <= {p.source for p in sc.probes()}


class TestTheChangeThatTaughtIt:
    """Findings item 132 replayed: EDGAR refuses a page size outside its list.
    The probes go through the scraper's own request, so the day a scraper sends
    a refused parameter the probe fails — and says which request was lost."""

    def _edgar(self, allowed):
        def get(url, params=None):
            if params["count"] not in allowed:
                return httpx.Response(400, text="Invalid count, Received: " + params["count"],
                                      request=httpx.Request("GET", url))
            body = (f"<feed><company-info><cik>{sc.COOK_CIK if params['type'] == '4' else APPLE}</cik>"
                    f"<conformed-name>{'COOK TIMOTHY D' if params['type'] == '4' else 'Apple Inc.'}"
                    "</conformed-name></company-info></feed>")
            return httpx.Response(200, text=body, request=httpx.Request("GET", url))
        client = MagicMock()
        client.get.side_effect = get
        return client

    def _name_probes(self):
        return [p for p in sc.probes() if "name search" in p.name]

    def test_the_name_searches_pass_against_todays_edgar(self):
        allowed = {str(n) for n in sec_client.BROWSE_PAGE_SIZES}
        with patch.object(sec_client, "_get_client", return_value=self._edgar(allowed)), patch("time.sleep"):
            assert sc.run(self._name_probes(), sleep=lambda s: None) == []

    def test_a_page_size_edgar_stops_taking_fails_both_and_names_the_request(self):
        with patch.object(sec_client, "_get_client", return_value=self._edgar({"500"})), patch("time.sleep"):
            failed = sc.run(self._name_probes(), sleep=lambda s: None)
        assert [p.name for p, _ in failed] == ["company-name search", "person-name search"]
        assert all("request lost: https://www.sec.gov/cgi-bin/browse-edgar?" in why and "count=10" in why
                   for _, why in failed)

"""An LEI confirmed by the filer's own filings (`app.scraper.sec_lei`).

The fixtures are trimmed from real EDGAR documents (2026-10-27):

- Unilever's 6-K directors'-dealing notice: one LEI, "Name Unilever PLC b) LEI";
- Sysco's 424B5: Sysco Corporation's LEI and Sysco Holdings' LEI, each in a
  sentence of its own — "sysco" alone stands before both;
- Fiserv's 424B5: "Issuer Legal Entity Identifier …" and "Guarantor Legal
  Entity Identifier …" — two LEIs, no company named at either;
- Kraft Heinz's 424B5: one LEI, the issuer's (Kraft Heinz Foods Company), in a
  document filed under the issuer's and the guarantor's CIK alike.
"""
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.scraper import sec_edgar, sec_lei
from app.scraper.sec_lei import (confirm_filer_lei, document_states_lei, lei_checksum_ok,
                                 leis_in, name_labels)

FIXTURES = Path(__file__).parent / "fixtures"

UNILEVER = "549300MKFYEKVRWML317"
SYSCO_CORP = "5RGWIFLMGH3YS7KWI652"
SYSCO_HOLDINGS = "254900HVBPYIWRRJXR33"
FISERV_ISSUER = "254900RNWK5SE9A0I712"
FISERV_GUARANTOR = "GI7UBEJLXYLGR2C7GV83"
KRAFT_FOODS = "5493003STKEZ2S0RNU91"


def text(name):
    return sec_edgar._plain_text((FIXTURES / name).read_text())


class TestTheChecksum:
    @pytest.mark.parametrize("code", [UNILEVER, SYSCO_CORP, SYSCO_HOLDINGS, FISERV_GUARANTOR,
                                      KRAFT_FOODS, "5493000C01ZX7D35SD85"])
    def test_real_leis_pass(self, code):
        assert lei_checksum_ok(code)

    @pytest.mark.parametrize("code", ["549300MKFYEKVRWML318",      # check digits off by one
                                      "549300MKFYEKVRWML371",      # check digits swapped
                                      "549300MKFYEKVRWML31",       # 19 characters
                                      "549300MKFYEKVRWML3-7", "", None])
    def test_look_alikes_fail(self, code):
        assert not lei_checksum_ok(code)

    def test_a_look_alike_in_a_text_is_no_lei(self):
        assert leis_in(f"ref 549300MKFYEKVRWML318 and LEI {UNILEVER}.") == [(UNILEVER, 33)]


class TestTheNameLabelsTheLei:
    def test_name_then_legal_form_and_label(self):
        assert name_labels("a) Name Unilever PLC b) LEI ", "UNILEVER PLC")

    def test_a_possessive_and_the_long_label(self):
        assert name_labels("Sysco Corporation’s Legal Entity Identifier is ", "SYSCO CORP")

    def test_a_further_word_after_the_name_is_another_company(self):
        assert not name_labels("Sysco Holdings’ Legal Entity Identifier is ", "SYSCO CORP")

    def test_only_the_last_mention_counts(self):
        assert not name_labels("Sysco Corporation owns Sysco Holdings. Its LEI is ", "SYSCO CORP")

    def test_a_name_of_legal_forms_only_names_nobody(self):
        assert not name_labels("The Company LEI ", "The Company")

    def test_accents_fold(self):
        assert name_labels("Nestlé S.A. LEI ", "NESTLE SA")


class TestReadingADocument:
    def test_unilever_one_lei_filed_alone(self):
        assert document_states_lei(text("unilever_6k_pdmr_lei_excerpt.htm"), UNILEVER, ["UNILEVER PLC"])

    def test_a_lei_the_document_does_not_hold(self):
        assert not document_states_lei(text("unilever_6k_pdmr_lei_excerpt.htm"), SYSCO_CORP, ["UNILEVER PLC"])

    def test_sysco_each_lei_belongs_to_the_name_that_labels_it(self):
        t = text("sysco_424b5_lei_excerpt.htm")
        assert document_states_lei(t, SYSCO_CORP, ["SYSCO CORP"])
        assert not document_states_lei(t, SYSCO_HOLDINGS, ["SYSCO CORP"])
        assert document_states_lei(t, SYSCO_HOLDINGS, ["Sysco Holdings, LLC"])

    def test_fiserv_two_leis_and_no_name_confirm_nothing(self):
        t = text("fiserv_424b5_lei_excerpt.htm")
        assert not document_states_lei(t, FISERV_GUARANTOR, ["FISERV INC"])
        assert not document_states_lei(t, FISERV_ISSUER, ["FISERV INC"])

    def test_kraft_heinz_a_shared_document_needs_the_name(self):
        t = text("kraft_heinz_424b5_lei_excerpt.htm")
        assert document_states_lei(t, KRAFT_FOODS, ["KRAFT HEINZ FOODS CO"], sole_filer=False)
        # the guarantor's CIK lists the same document; its name labels nothing
        assert not document_states_lei(t, KRAFT_FOODS, ["KRAFT HEINZ CO"], sole_filer=False)
        # filed alone, the only LEI would have been taken
        assert document_states_lei(t, KRAFT_FOODS, ["KRAFT HEINZ CO"], sole_filer=True)

    def test_a_name_too_far_before_the_lei(self):
        t = f"Unilever PLC b) {'x ' * 200}LEI {UNILEVER} and {SYSCO_CORP}"
        assert not document_states_lei(t, UNILEVER, ["UNILEVER PLC"])

    def test_a_name_labels_only_the_code_right_after_it(self):
        # "Unilever PLC" labels the first code; the second has no name of its own
        t = f"Name Unilever PLC LEI {SYSCO_CORP} LEI {UNILEVER}"
        assert not document_states_lei(t, UNILEVER, ["UNILEVER PLC"])


def hit(cik_list, hit_id="0000217410-23-000039:a991-directorpdmrshareho.htm", form="6-K"):
    return {"_id": hit_id, "_source": {"ciks": cik_list, "form": form, "file_date": "2023-09-01"}}


class TestConfirmFilerLei:
    UNILEVER_DOC = (FIXTURES / "unilever_6k_pdmr_lei_excerpt.htm").read_text()

    def test_a_confirmed_lei_comes_with_its_filing(self):
        with patch.object(sec_edgar, "_get", return_value={"hits": {"hits": [hit(["0000217410"])]}}) as get, \
             patch.object(sec_edgar, "_get_text", return_value=self.UNILEVER_DOC) as read:
            out = confirm_filer_lei("217410", UNILEVER, ["UNILEVER PLC"])
        assert get.call_args.args[1] == {"q": f'"{UNILEVER}"', "ciks": "0000217410"}
        read.assert_called_once_with(
            "https://www.sec.gov/Archives/edgar/data/217410/000021741023000039/a991-directorpdmrshareho.htm")
        assert out == {"cik": "0000217410", "lei": UNILEVER, "form": "6-K", "file_date": "2023-09-01",
                       "source_url": read.call_args.args[0]}

    def test_a_hit_not_filed_under_the_cik_is_not_read(self):
        with patch.object(sec_edgar, "_get", return_value={"hits": {"hits": [hit(["0000320193"])]}}), \
             patch.object(sec_edgar, "_get_text") as read:
            assert confirm_filer_lei("217410", UNILEVER, ["UNILEVER PLC"]) is None
        read.assert_not_called()

    def test_a_shared_hit_needs_the_filers_name(self):
        kraft = (FIXTURES / "kraft_heinz_424b5_lei_excerpt.htm").read_text()
        shared = hit(["0000046640", "0001637459"], "0001193125-25-030026:d930629d424b5.htm", "424B5")
        with patch.object(sec_edgar, "_get", return_value={"hits": {"hits": [shared]}}), \
             patch.object(sec_edgar, "_get_text", return_value=kraft):
            assert confirm_filer_lei("1637459", KRAFT_FOODS, ["KRAFT HEINZ CO"]) is None
            assert confirm_filer_lei("46640", KRAFT_FOODS, ["KRAFT HEINZ FOODS CO"])["cik"] == "0000046640"

    def test_at_most_max_documents_are_read(self):
        hits = [hit(["0000217410"], f"0000217410-23-00003{i}:doc{i}.htm") for i in range(5)]
        with patch.object(sec_edgar, "_get", return_value={"hits": {"hits": hits}}), \
             patch.object(sec_edgar, "_get_text", return_value="no code here") as read:
            assert confirm_filer_lei("217410", UNILEVER, ["UNILEVER PLC"]) is None
        assert read.call_count == sec_lei.MAX_DOCUMENTS

    def test_an_unreadable_document_moves_on_to_the_next(self):
        hits = [hit(["0000217410"], "0000217410-23-000038:gone.htm"), hit(["0000217410"])]
        err = httpx.HTTPStatusError("404", request=httpx.Request("GET", "x"),
                                    response=httpx.Response(404))
        with patch.object(sec_edgar, "_get", return_value={"hits": {"hits": hits}}), \
             patch.object(sec_edgar, "_get_text", side_effect=[err, self.UNILEVER_DOC]):
            assert confirm_filer_lei("217410", UNILEVER, ["UNILEVER PLC"])["lei"] == UNILEVER

    def test_a_failed_search_is_unknown_not_a_failure(self):
        with patch.object(sec_edgar, "_get", side_effect=httpx.ConnectError("down")):
            assert confirm_filer_lei("217410", UNILEVER, ["UNILEVER PLC"]) is None

    def test_no_request_for_a_code_that_is_no_lei(self):
        with patch.object(sec_edgar, "_get") as get:
            assert confirm_filer_lei("217410", "549300MKFYEKVRWML318", ["UNILEVER PLC"]) is None
            assert confirm_filer_lei(None, UNILEVER, ["UNILEVER PLC"]) is None
        get.assert_not_called()

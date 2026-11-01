"""A new node's id carries the register's key when the writer knows one
(app.node_ids, 2026-10-26)."""
import uuid

from unittest.mock import MagicMock, patch

import pytest

from app.node_ids import key_on_learning, keyed_id, mint_node_id


class TestMinting:
    def test_the_lei_outranks_the_cik_outranks_the_item(self):
        # a register's id before a knowledge base's (the user's call, 2026-10-30)
        assert keyed_id("529900T8BM49AURSDO55", "Q312", "0000320193") == "lei:529900T8BM49AURSDO55"
        assert keyed_id(None, "Q312", "0000320193") == "sec:0000320193"
        assert keyed_id(None, "Q312", None) == "wd:Q312"
        assert keyed_id(None, None, "0000320193") == "sec:0000320193"
        assert keyed_id(None, None, "320193") == "sec:0000320193"          # one filer, one id: the padded form

    def test_a_name_only_node_gets_a_uuid(self):
        assert keyed_id(None, None, None) is None
        assert uuid.UUID(mint_node_id()).version == 4
        assert mint_node_id(wikidata_id="Q1") == "wd:Q1"

    def test_a_malformed_key_is_not_keyed_on(self):
        """A merge key must never be minted from a value that is not one."""
        assert keyed_id("not-an-lei", None, None) is None
        assert keyed_id(None, "Apple", None) is None
        assert keyed_id(None, None, "CIK 320193") is None
        assert keyed_id(None, " Q7 ", None) == "wd:Q7"                  # whitespace tolerated, not kept


class TestKeyOnLearning:
    """A node takes the best id its hard ids give it, whichever source wrote
    first (findings item 20, 2026-10-30)."""
    UUID = "33333333-3333-4333-8333-333333333333"

    def _learn(self, node_id, held, renamed=True):
        session = MagicMock()
        session.run.return_value.single.return_value = held
        with patch("app.merged_ids.rename_node_id", return_value=renamed) as rename:
            got = key_on_learning(session, "Entity", node_id)
        return got, rename

    @pytest.mark.parametrize("node_id, held, want", [
        (UUID, {"lei": None, "wid": "Q1", "cik": None}, "wd:Q1"),
        ("wd:Q28222763", {"lei": None, "wid": "Q28222763", "cik": "0000912093"}, "sec:0000912093"),   # Viavi
        ("sec:0001011006", {"lei": "549300ZJC0V1K2ZCKG85", "wid": "Q30321616", "cik": "0001011006"},
         "lei:549300ZJC0V1K2ZCKG85"),                                                                    # Altaba
        ("wd:Q1", {"lei": "549300ZJC0V1K2ZCKG85", "wid": "Q1", "cik": None}, "lei:549300ZJC0V1K2ZCKG85"),
    ])
    def test_a_better_id_is_taken(self, node_id, held, want):
        got, rename = self._learn(node_id, held)
        assert got == want
        rename.assert_called_once()
        assert rename.call_args.args[2:] == (node_id, want)

    @pytest.mark.parametrize("node_id, held", [
        ("sec:0001326801", {"lei": None, "wid": "Q380", "cik": "0001326801"}),     # a QID ranks below
        ("wd:Q1", {"lei": None, "wid": "Q1", "cik": None}),
        ("sec:0000000001", {"lei": None, "wid": "Q1", "cik": None}),          # never a downgrade
    ])
    def test_an_equal_or_worse_id_is_kept(self, node_id, held):
        got, rename = self._learn(node_id, held)
        assert got == node_id
        rename.assert_not_called()

    @pytest.mark.parametrize("label, node_id", [("Entity", "lei:549300ZJC0V1K2ZCKG85"),
                                                ("Entity", "gb-coh:00041424"),
                                                ("Person", "chpsc:03977902:cdqMtbUIfvMc4RgPpHEhBM8trCs")])
    def test_the_best_or_a_register_key_is_not_even_looked_up(self, label, node_id):
        session = MagicMock()
        assert key_on_learning(session, label, node_id) == node_id
        session.run.assert_not_called()

    @pytest.mark.parametrize("held, want", [
        ({"lei": "5493000C01ZX7D35SD85", "wid": "Q217583", "cik": "0001067983"}, "lei:5493000C01ZX7D35SD85"),
        ({"lei": None, "wid": None, "cik": "0001067983"}, "sec:0001067983"),
    ])
    def test_a_corporate_psc_takes_its_hard_id(self, held, want):
        # findings item 26: the merge kept Berkshire's PSC node, which held the LEI
        got, rename = self._learn("chpsc:03230337:VkVz5Am9-v9NnalMlijnYSGP0jE", held)
        assert got == want
        rename.assert_called_once()

    def test_a_corporate_psc_without_a_hard_id_keeps_its_id(self):
        got, rename = self._learn("chpsc:03230337:VkVz5Am9-v9NnalMlijnYSGP0jE",
                                  {"lei": None, "wid": None, "cik": None})
        assert got == "chpsc:03230337:VkVz5Am9-v9NnalMlijnYSGP0jE"
        rename.assert_not_called()

    def test_a_taken_id_is_a_twin_not_forced(self):
        got, _ = self._learn("sec:0001011006", {"lei": "549300ZJC0V1K2ZCKG85", "wid": None, "cik": "0001011006"},
                             renamed=False)
        assert got == "sec:0001011006"

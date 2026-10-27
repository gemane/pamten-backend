"""A new node's id carries the register's key when the writer knows one
(app.node_ids, 2026-10-26)."""
import uuid

from app.node_ids import keyed_id, mint_node_id


class TestMinting:
    def test_the_lei_outranks_the_item_outranks_the_cik(self):
        assert keyed_id("529900T8BM49AURSDO55", "Q312", "0000320193") == "lei:529900T8BM49AURSDO55"
        assert keyed_id(None, "Q312", "0000320193") == "wd:Q312"
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

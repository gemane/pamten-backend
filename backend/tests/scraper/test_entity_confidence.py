"""Confidence tiers for same-name entity groups (drives auto-merge vs review).

The full merge is covered against a real ArcadeDB in
tests/integration/test_entity_dedup_it.py."""

from app.scraper.maintenance import _group_confidence


def _m(**kw):
    return kw


class TestGroupConfidence:
    def test_shared_hard_id_is_definitive(self):
        # any shared hard external id ⇒ same company (no name/Wikidata needed)
        assert _group_confidence([_m(lei_id="X"), _m(lei_id="X")]) == "definitive"
        assert _group_confidence([_m(sec_cik="789019"), _m(sec_cik="789019")]) == "definitive"
        assert _group_confidence([_m(companies_house_id="07434180"),
                                  _m(companies_house_id="07434180")]) == "definitive"

    def test_same_registered_address_is_high(self):
        assert _group_confidence([_m(registered_address="1 a st london gb"),
                                  _m(registered_address="1 a st london gb")]) == "high"

    def test_distinct_ids_not_definitive(self):
        # different LEIs with nothing else in common ⇒ not a confident merge
        assert _group_confidence([_m(lei_id="X"), _m(lei_id="Y")]) == "low"


class TestWikidataGleifBridge:
    """The Microsoft case: why the pair used to sit unmerged, and what changes."""

    GLEIF    = dict(lei_id="INR2EJN1ERAN0W5ZP974", country="US", registered_address="One Microsoft Way")
    WIKIDATA = dict(wikidata_id="Q2283", sec_cik="0000789019", country="US")

    def test_pair_without_a_shared_id_is_not_auto_merged(self):
        # What we had: GLEIF holds lei_id, Wikidata holds wikidata_id + sec_cik.
        # No overlap, so nothing above "medium" — below the auto-merge threshold,
        # which is why one node had the ownership graph and the other the
        # executives, with no way for a user to see both.
        verdict = _group_confidence([self.GLEIF, self.WIKIDATA])
        assert verdict != "definitive"

    def test_lei_from_wikidata_makes_the_pair_definitive(self):
        # With P1278 read from Wikidata the two carry the same lei_id, which is
        # exactly the signal the dedup already looks for.
        bridged = {**self.WIKIDATA, "lei_id": "INR2EJN1ERAN0W5ZP974"}
        assert _group_confidence([self.GLEIF, bridged]) == "definitive"

    def test_a_differing_lei_is_not_definitive(self):
        # Two genuinely different companies that happen to share a name must not
        # be merged just because both now carry an LEI.
        other = {**self.WIKIDATA, "lei_id": "549300G0CFPGEF6X2043"}
        assert _group_confidence([self.GLEIF, other]) != "definitive"


# ── What survives an entity merge ─────────────────────────────────────────────
#
# The merge used to migrate EDGES only and delete the loser outright, so folding a
# GLEIF node together with its Wikidata twin kept the ownership graph but threw
# away wikidata_id, sec_cik, the description, the revenue and the headcount.
# Losing wikidata_id also un-marked the company as "notable" for search ranking
# and removed the key a later Wikidata scrape resolves on.

from app.scraper.maintenance import _merge_entity_props  # noqa: E402


class TestMergeEntityProps:
    KEEP = {"id": "lei:X", "name": "MICROSOFT CORPORATION", "lei_id": "INR2EJN1ERAN0W5ZP974",
            "name_credibility": 92, "country": "US"}
    DEAD = {"id": "uuid-1", "name": "Microsoft", "wikidata_id": "Q2283",
            "sec_cik": "0000789019", "description": "American technology company",
            "revenue": 2.11e11, "employees": 228000, "name_credibility": 80}

    def test_identifiers_are_carried_across(self):
        out = _merge_entity_props(self.KEEP, self.DEAD)
        assert out["wikidata_id"] == "Q2283"
        assert out["sec_cik"] == "0000789019"

    def test_descriptive_fields_are_carried_across(self):
        out = _merge_entity_props(self.KEEP, self.DEAD)
        assert out["description"] == "American technology company"
        assert out["employees"] == 228000
        assert out["revenue"] == self.DEAD["revenue"]

    def test_survivor_values_are_never_clobbered(self):
        # The survivor was chosen deliberately; its own data wins.
        keep = {**self.KEEP, "description": "The kept description", "country": "US"}
        dead = {**self.DEAD, "country": "DE"}
        out = _merge_entity_props(keep, dead)
        assert "description" not in out or out["description"] == "The kept description"
        assert "country" not in out

    def test_survivor_identity_is_untouched(self):
        out = _merge_entity_props(self.KEEP, self.DEAD)
        for protected in ("id", "name", "name_normalized"):
            assert protected not in out

    def test_the_losers_name_becomes_an_alias(self):
        # Or the company stops being findable under the name it just absorbed.
        out = _merge_entity_props(self.KEEP, self.DEAD)
        assert "Microsoft" in out["aliases"]

    def test_survivors_own_name_is_not_listed_as_its_alias(self):
        out = _merge_entity_props({**self.KEEP, "name": "Microsoft"}, self.DEAD)
        assert [a for a in out.get("aliases", []) if a.lower() == "microsoft"] == []

    def test_lists_are_unioned_without_duplicates(self):
        keep = {**self.KEEP, "aliases": ["MSFT"], "countries": ["US"]}
        dead = {**self.DEAD, "aliases": ["MSFT", "Micro-Soft"], "countries": ["US", "IE"]}
        out = _merge_entity_props(keep, dead)
        assert out["aliases"].count("MSFT") == 1
        assert set(out["countries"]) == {"US", "IE"}

    def test_search_text_covers_the_merged_aliases(self):
        # The FULL_TEXT column has to include an absorbed name, or search can't
        # find the company under it.
        out = _merge_entity_props(self.KEEP, self.DEAD)
        assert "Microsoft" in out["search_text"]
        assert "MICROSOFT CORPORATION" in out["search_text"]

    def test_credibility_takes_the_higher_value(self):
        assert _merge_entity_props(self.KEEP, self.DEAD)["name_credibility"] == 92
        assert _merge_entity_props(self.DEAD, self.KEEP)["name_credibility"] == 92

    def test_verified_survives_from_either_side(self):
        assert _merge_entity_props(self.KEEP, {**self.DEAD, "verified": True})["verified"] is True
        assert _merge_entity_props({**self.KEEP, "verified": True}, self.DEAD)["verified"] is True

    def test_empty_values_do_not_overwrite_with_blanks(self):
        keep = {**self.KEEP, "description": "kept"}
        dead = {**self.DEAD, "description": "", "employees": None}
        out = _merge_entity_props(keep, dead)
        assert out.get("description", "kept") == "kept"
        assert "employees" not in out

    def test_unknown_future_fields_are_carried_too(self):
        # Deny-list rather than allow-list: a field a future scraper adds should
        # survive a merge without anyone remembering to update the merge code.
        out = _merge_entity_props(self.KEEP, {**self.DEAD, "brand_new_field": "value"})
        assert out["brand_new_field"] == "value"

    def test_arcadedb_metadata_is_not_copied(self):
        out = _merge_entity_props(self.KEEP, {**self.DEAD, "@rid": "#1:2", "@type": "Entity"})
        assert not any(k.startswith("@") for k in out)

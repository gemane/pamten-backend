"""Claim identity and which claim the edge should believe.

The database side is covered by tests/integration/test_claims_it.py; these are
the pure decisions — how a claim is keyed, and how a conflict is resolved.
"""
import pytest

from app.claims import (
    KIND_OWNS, KIND_ROLE, best_claim, claim_key, claim_props, edge_values_from,
)


def claim(source_id="gleif", cred=90, date="2026-01-01", stake=None):
    return {"source_id": source_id, "credibility_score": cred,
            "source_date": date, "stake_percent": stake}


# ── Identity ──────────────────────────────────────────────────────────────────

class TestClaimKey:
    def test_same_assertion_from_the_same_source_is_one_claim(self):
        """This is what makes a re-import update instead of accumulating."""
        assert claim_key(KIND_OWNS, "A", "B", "gleif") == claim_key(KIND_OWNS, "A", "B", "gleif")

    def test_a_different_source_is_a_different_claim(self):
        assert claim_key(KIND_OWNS, "A", "B", "gleif") != claim_key(KIND_OWNS, "A", "B", "ch-psc")

    def test_direction_matters(self):
        # A owning B is not B owning A, and both can be true at once.
        assert claim_key(KIND_OWNS, "A", "B", "gleif") != claim_key(KIND_OWNS, "B", "A", "gleif")

    def test_kind_matters(self):
        assert claim_key(KIND_OWNS, "A", "B", "x") != claim_key(KIND_ROLE, "A", "B", "x")

    def test_separator_cannot_be_forged_across_fields(self):
        # Naive concatenation would make ("A|B", "C") collide with ("A", "B|C").
        assert claim_key(KIND_OWNS, "A|B", "C", "s") != claim_key(KIND_OWNS, "A", "B|C", "s")


class TestClaimProps:
    def test_carries_the_key_and_the_assertion(self):
        p = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                        stake_percent=60.0, credibility_score=90)
        assert p["claim_key"] == claim_key(KIND_OWNS, "A", "B", "gleif")
        assert p["stake_percent"] == 60.0
        assert p["credibility_score"] == 90

    def test_omits_first_seen_at(self):
        """It must survive updates, so the writers set it with COALESCE against
        the stored value; passing it here would overwrite on every re-import."""
        assert "first_seen_at" not in claim_props(
            kind=KIND_OWNS, from_id="A", to_id="B", source_id="s")
        assert "last_seen_at" in claim_props(
            kind=KIND_OWNS, from_id="A", to_id="B", source_id="s")


# ── Conflict resolution ───────────────────────────────────────────────────────

class TestBestClaim:
    def test_no_claims_no_winner(self):
        assert best_claim([]) is None

    def test_the_more_credible_source_wins(self):
        gleif = claim("gleif", cred=90, stake=60.0)
        psc   = claim("ch-psc", cred=85, stake=75.0)
        assert best_claim([psc, gleif])["source_id"] == "gleif"

    def test_credibility_beats_recency(self):
        # A newer figure from a weaker source does not displace a stronger one.
        strong_old = claim("gleif", cred=90, date="2020-01-01")
        weak_new   = claim("blog",  cred=10, date="2026-08-01")
        assert best_claim([weak_new, strong_old])["source_id"] == "gleif"

    def test_recency_breaks_a_credibility_tie(self):
        older = claim("a", cred=90, date="2024-01-01")
        newer = claim("b", cred=90, date="2026-01-01")
        assert best_claim([older, newer])["source_id"] == "b"

    def test_an_undated_claim_loses_a_tie_rather_than_winning_it(self):
        dated   = claim("a", cred=90, date="2024-01-01")
        undated = claim("b", cred=90, date=None)
        assert best_claim([undated, dated])["source_id"] == "a"

    def test_a_credible_claim_without_a_stake_still_wins(self):
        """Deliberate: "owns, amount undisclosed" from a strong source describes
        what is known better than a weak source's invented-looking number."""
        strong_no_pct = claim("gleif", cred=95, stake=None)
        weak_with_pct = claim("blog",  cred=20, stake=42.0)
        assert best_claim([weak_with_pct, strong_no_pct])["source_id"] == "gleif"

    def test_among_registers_a_stated_stake_beats_a_silent_list(self):
        """The UK register's 75% over SEC's Exhibit 21, which names the
        subsidiary with no number — the rule the shared edge follows too
        (app.scraper.owns_merge)."""
        ex21 = claim("sec", cred=98, stake=None)
        psc = claim("ch-psc", cred=97, stake=75.0)
        assert best_claim([ex21, psc])["source_id"] == "ch-psc"

    def test_missing_credibility_is_treated_as_zero_not_an_error(self):
        assert best_claim([{"source_id": "x"}, claim("y", cred=1)])["source_id"] == "y"


class TestEdgeValues:
    def test_takes_the_winner_s_values(self):
        gleif = {**claim("gleif", cred=90, stake=60.0), "ownership_type": "majority"}
        psc   = {**claim("ch-psc", cred=85, stake=75.0), "ownership_type": "majority"}
        values = edge_values_from([psc, gleif])
        assert values["stake_percent"] == 60.0
        assert values["source_id"] == "gleif"

    def test_empty_for_no_claims(self):
        assert edge_values_from([]) == {}

    @pytest.mark.parametrize("field", [
        "stake_percent", "voting_power_pct", "ownership_type", "since", "until",
        "source_id", "source_url", "source_date", "credibility_score",
    ])
    def test_carries_every_field_the_edge_needs(self, field):
        assert field in edge_values_from([claim()])


# ── Acquisition claims: SEC's second assertion about a pair ───────────────────

class TestAcquisitionClaimKey:
    """An owns claim read from the acquirer's XBRL (filing_type "10-Q XBRL") is
    keyed APART from the same source's Exhibit 21 claim for the pair: the list
    says "held at year-end", the deal says "bought on this day". Keyed together
    the two overwrote each other on every re-read."""

    def test_an_acquisition_claim_is_a_second_claim_for_the_pair(self):
        from app.claims import ACQUISITION_FILING_TYPES
        plain = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec", filing_type="EX-21")
        for ft in sorted(ACQUISITION_FILING_TYPES):
            deal = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec", filing_type=ft)
            assert deal["claim_key"] != plain["claim_key"]
        assert claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec",
                           filing_type="10-K XBRL")["claim_key"] == \
            claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec",
                        filing_type="10-Q XBRL")["claim_key"], "one acquisition claim per pair, whichever form"

    def test_every_sec_filing_family_is_its_own_claim_for_the_pair(self):
        """2026-10-26: a Form 4, a 13G and a 13F about one pair are three
        statements of the register, keyed apart; an amendment of the same
        family updates its own claim."""
        from app.claims import filing_family
        keys = {ft: claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec",
                                filing_type=ft)["claim_key"]
                for ft in ("Form 4", "Form 3", "13G", "13G/A", "13D/A", "SC 13D", "13F", "EX-21", "EX-8.1", "10-Q XBRL")}
        assert keys["Form 4"] == keys["Form 3"]                       # the insider statements
        assert keys["13G"] == keys["13G/A"] == keys["13D/A"] == keys["SC 13D"]
        assert len({keys["Form 4"], keys["13G"], keys["13F"], keys["EX-21"], keys["EX-8.1"], keys["10-Q XBRL"]}) == 6
        assert filing_family("13G/A") == "13dg" and filing_family("Form 4") == "insider"
        assert filing_family("EX-21") == "ex21" and filing_family("13F") == "13f"

    def test_another_sources_filing_type_leaves_the_plain_key(self):
        from app.claims import filing_family, owns_claim_key
        plain = claim_key(KIND_OWNS, "A", "B", "gleif")
        for ft in ("RR", "snapshot", "", None):
            assert claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                               filing_type=ft)["claim_key"] == plain
            assert filing_family(ft) is None
        assert owns_claim_key("A", "B", "gleif", "RR") == plain
        assert owns_claim_key("A", "B", "sec", "EX-21") == \
            claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec", filing_type="EX-21")["claim_key"]

    def test_key_for_reproduces_the_stored_key(self):
        from app.claims import key_for
        deal = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec", filing_type="10-Q XBRL")
        assert key_for(deal) == deal["claim_key"], "a heal must never fold it into the plain claim"
        plain = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec", filing_type="EX-21")
        assert key_for(plain) == plain["claim_key"]
        g = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="sec", filing_type="13G/A")
        assert key_for(g) == g["claim_key"] != plain["claim_key"]
        # a claim written before 2026-10-26 under the plain key is re-keyed by the heal
        old = {**g, "claim_key": claim_key(KIND_OWNS, "A", "B", "sec")}
        assert key_for(old) == g["claim_key"]


class TestHowFarTheRegisterCheckedIt:
    def test_the_claim_carries_the_corroboration_level_as_stated(self):
        from app.claims import KIND_OWNS, claim_props
        p = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                        validation_sources="ENTITY_SUPPLIED_ONLY",
                        validation_documents="OTHER_OFFICIAL_DOCUMENTS", credibility_score=92)
        assert p["validation_sources"] == "ENTITY_SUPPLIED_ONLY"
        assert p["validation_documents"] == "OTHER_OFFICIAL_DOCUMENTS"
        assert p["credibility_score"] == 92             # provenance, not a penalty
        bare = claim_props(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif")
        assert bare["validation_sources"] is None and bare["validation_documents"] is None

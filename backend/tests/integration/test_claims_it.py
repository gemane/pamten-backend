"""Real-ArcadeDB tests for per-source claims.

The unit tests cover keying and conflict resolution as pure logic. These cover
what only a real database can answer: that the UPSERT actually respects the
UNIQUE index instead of inserting duplicates, that `COALESCE(first_seen_at, …)`
works inside a SQL UPDATE (the Cypher dialect's limitations mean that cannot be
assumed), that numbers survive the round trip as numbers, and that two sources
asserting the same relationship really do end up as two rows.
"""
import pytest

from app.claims import KIND_OWNS, KIND_ROLE, best_claim, claim_key, claims_for, record_claim
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration


def _count() -> int:
    return run_sql("SELECT count(*) AS n FROM Claim")[0]["n"]


# ── The row round-trips ───────────────────────────────────────────────────────

def test_a_claim_is_written_and_readable(it_db):
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                 stake_percent=60.0, ownership_type="majority", credibility_score=90)
    rows = claims_for(from_id="A", to_id="B")
    assert len(rows) == 1
    assert rows[0]["source_id"] == "gleif"
    assert rows[0]["ownership_type"] == "majority"


def test_a_percentage_comes_back_as_a_number(it_db):
    """Stored as a string, every comparison and sum downstream would be wrong."""
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                 stake_percent=60.5)
    assert claims_for(from_id="A", to_id="B")[0]["stake_percent"] == 60.5


def test_the_schema_bootstrap_creates_the_type(it_db):
    assert _count() == 0


# ── Re-imports do not accumulate ──────────────────────────────────────────────

def test_the_same_source_re_asserting_updates_its_claim(it_db):
    """The whole point of keying on (kind, from, to, source): unlike the edges,
    which ArcadeDB can only CREATE and which need a later dedup pass, a re-import
    is idempotent here."""
    for pct in (60.0, 62.0, 65.0):
        record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                     stake_percent=pct)
    assert _count() == 1
    assert claims_for(from_id="A", to_id="B")[0]["stake_percent"] == 65.0


def test_first_seen_survives_an_update_while_last_seen_moves(it_db):
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif")
    first = claims_for(from_id="A", to_id="B")[0]
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                 stake_percent=70.0)
    second = claims_for(from_id="A", to_id="B")[0]

    assert second["first_seen_at"] == first["first_seen_at"]
    assert second["last_seen_at"] >= first["last_seen_at"]


def test_a_claim_without_a_source_is_not_written(it_db):
    # The key is (kind, from, to, source); an unsourced claim would collide with
    # every other unsourced claim about the same pair.
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="")
    assert _count() == 0


# ── The reason it exists ──────────────────────────────────────────────────────

def test_two_sources_disagreeing_are_both_kept(it_db):
    """Previously the second writer overwrote the first and the disagreement
    was invisible. GLEIF and Companies House can both be right about what their
    own register says."""
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                 stake_percent=60.0, credibility_score=90, source_date="2026-01-01")
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="ch-psc",
                 stake_percent=75.0, credibility_score=85, source_date="2026-06-01")

    rows = claims_for(from_id="A", to_id="B")
    assert {r["source_id"] for r in rows} == {"gleif", "ch-psc"}
    assert {r["stake_percent"] for r in rows} == {60.0, 75.0}


def test_the_more_credible_source_is_the_one_the_edge_should_carry(it_db):
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="gleif",
                 stake_percent=60.0, credibility_score=90)
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="ch-psc",
                 stake_percent=75.0, credibility_score=85)
    assert best_claim(claims_for(from_id="A", to_id="B"))["source_id"] == "gleif"


def test_claims_are_returned_most_credible_first(it_db):
    for src, cred in (("weak", 10), ("strong", 95), ("middling", 50)):
        record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id=src,
                     credibility_score=cred)
    assert [r["source_id"] for r in claims_for(from_id="A", to_id="B")] == \
        ["strong", "middling", "weak"]


# ── Selection ─────────────────────────────────────────────────────────────────

def test_kinds_are_separate_claims_on_the_same_pair(it_db):
    record_claim(kind=KIND_OWNS, from_id="P", to_id="E", source_id="s")
    record_claim(kind=KIND_ROLE, from_id="P", to_id="E", source_id="s", role="CEO")
    assert _count() == 2
    assert len(claims_for(from_id="P", to_id="E", kind=KIND_ROLE)) == 1


def test_selecting_by_target_finds_what_is_asserted_about_an_entity(it_db):
    """How the Sources panel reads: everything claimed *about* this entity, and
    nothing about the subsidiaries it owns."""
    record_claim(kind=KIND_OWNS, from_id="OWNER", to_id="ME", source_id="s1")
    record_claim(kind=KIND_OWNS, from_id="ME", to_id="SUBSIDIARY", source_id="s2")

    about_me = claims_for(to_id="ME")
    assert [c["from_id"] for c in about_me] == ["OWNER"]


def test_direction_is_not_conflated(it_db):
    record_claim(kind=KIND_OWNS, from_id="A", to_id="B", source_id="s")
    record_claim(kind=KIND_OWNS, from_id="B", to_id="A", source_id="s")
    assert _count() == 2
    assert claim_key(KIND_OWNS, "A", "B", "s") != claim_key(KIND_OWNS, "B", "A", "s")

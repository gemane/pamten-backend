"""Real-ArcadeDB tests for section counts and the ownership-tree edge filter.

Both are aggregation and variable-length Cypher, which is the class of thing a
mocked session cheerfully accepts and a real database rejects. The counts also
have to be *right*, not merely present: a number that disagrees with the list it
labels is worse than no number.
"""
import pytest

from app.routers.relationships import ownership_tree_of
from app.routers.search import get_full_profile

pytestmark = pytest.mark.integration


def _company(it_db, eid: str, name: str) -> None:
    it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company'}})")


def _owns(it_db, parent: str, child: str, doi: str | None = None) -> None:
    marker = f", direct_or_indirect: '{doi}'" if doi else ""
    it_db.run_command(
        f"MATCH (a:Entity {{id: '{parent}'}}), (b:Entity {{id: '{child}'}}) "
        f"CREATE (a)-[:OWNS {{until: null, source_id: 's'{marker}}}]->(b)")


# ── Counts ────────────────────────────────────────────────────────────────────

def test_counts_report_the_true_total(it_db):
    _company(it_db, "p", "Parent")
    for i in range(7):
        _company(it_db, f"s{i}", f"Sub {i}")
        _owns(it_db, "p", f"s{i}")

    assert get_full_profile("p")["counts"]["subsidiaries"] == 7


def test_counts_are_independent_of_the_row_limit(it_db):
    """The whole reason they come from the server: an array length is a lower
    bound once the section is capped."""
    _company(it_db, "p", "Parent")
    for i in range(7):
        _company(it_db, f"s{i}", f"Sub {i}")
        _owns(it_db, "p", f"s{i}")

    profile = get_full_profile("p", limit=3)
    assert len(profile["subsidiaries"]) == 3      # the list is capped
    assert profile["counts"]["subsidiaries"] == 7  # the count is not


def test_duplicate_edges_count_once(it_db):
    """Re-imports leave duplicate OWNS edges — Johnson & Johnson had 236 edges to
    160 subsidiaries — so counting edges would print a number the list disagrees
    with."""
    _company(it_db, "p", "Parent")
    _company(it_db, "s", "Sub")
    _owns(it_db, "p", "s")
    _owns(it_db, "p", "s")

    profile = get_full_profile("p")
    assert profile["counts"]["subsidiaries"] == 1
    assert len(profile["subsidiaries"]) == 1


def test_counts_cover_every_section(it_db):
    _company(it_db, "p", "Parent")
    profile = get_full_profile("p")
    for section in ("owners", "subsidiaries", "executives",
                    "dual_listed", "succeeded_by", "replaces"):
        assert profile["counts"][section] == 0, section


def test_closed_relationships_are_not_counted(it_db):
    """`until` set means the holding ended; the list excludes it, so must the count."""
    _company(it_db, "p", "Parent")
    _company(it_db, "s", "Sub")
    it_db.run_command(
        "MATCH (a:Entity {id:'p'}), (b:Entity {id:'s'}) "
        "CREATE (a)-[:OWNS {until: '2020-01-01', source_id: 's'}]->(b)")

    profile = get_full_profile("p")
    assert profile["counts"]["subsidiaries"] == 0
    assert profile["subsidiaries"] == []


# ── The ownership-tree edge filter ────────────────────────────────────────────

def _chain(it_db):
    """parent -> mid -> leaf by direct edges, plus a shortcut parent -> leaf."""
    for eid, name in (("parent", "Parent"), ("mid", "Mid"), ("leaf", "Leaf")):
        _company(it_db, eid, name)
    _owns(it_db, "parent", "mid", "direct")
    _owns(it_db, "mid", "leaf", "direct")
    _owns(it_db, "parent", "leaf", "indirect")     # GLEIF's ultimate-parent shortcut


def test_shortcut_edges_are_included_by_default(it_db):
    """The default is inclusive on purpose. Excluding by KIND removed companies
    whose only link is an ultimate-parent edge; whether a given shortcut is
    redundant is decided by maintenance.mark_ownership_shortcuts, not here."""
    _chain(it_db)
    paths, _ = ownership_tree_of("parent", depth=3)
    assert len(paths) == 3


def test_nothing_is_unreachable_either_way(it_db):
    _chain(it_db)
    for include in (True, False):
        paths, _ = ownership_tree_of("parent", depth=3, include_indirect=include)
        reached = {n["id"] for p in paths for n in p["nodes"]}
        assert "leaf" in reached, f"lost the leaf with include_indirect={include}"


def test_the_opt_out_still_filters(it_db):
    _chain(it_db)
    paths, _ = ownership_tree_of("parent", depth=3, include_indirect=False)
    assert len(paths) == 2


def test_the_filter_applies_to_every_hop_not_just_the_last(it_db):
    """A plain WHERE would test the final edge only and let a shortcut back in
    partway down a chain."""
    for eid in ("a", "b", "c"):
        _company(it_db, eid, eid.upper())
    _owns(it_db, "a", "b", "indirect")   # shortcut on the FIRST hop
    _owns(it_db, "b", "c", "direct")     # legitimate on the last

    paths, _ = ownership_tree_of("a", depth=3, include_indirect=False)
    assert paths == []


def test_edges_without_the_field_are_kept(it_db):
    """Wikidata and SEC never state the distinction. Absent is not indirect, and
    dropping them would lose the only ownership those sources record."""
    _company(it_db, "p", "Parent")
    _company(it_db, "s", "Sub")
    _owns(it_db, "p", "s")               # no direct_or_indirect at all

    paths, _ = ownership_tree_of("p", depth=2)
    assert len(paths) == 1

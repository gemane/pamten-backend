"""
One role, one edge — even when two scrapes describe it differently.

Larry Page and Elon Musk each appeared twice on the same board after being
scraped from both directions. Two causes, both here:

* the person path spelled the role "Board member" while the company path writes
  "Board Member", and two strings are two edges;
* the company path knows *when* someone joined ("since 1998") and the person path
  does not, and the dedup key was role **plus** since — so an undated assertion
  of a role already recorded created a second edge beside it.

A spell that ended before the next began is still its own edge: people do
hold the same post twice. Two OPEN assertions of one post are one seat,
however the two sources date its start (2026-10-25): a Form 3 filed three
days after Wikidata's appointment day used to open a second "Director" seat.
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def graph(it_db):
    from app.scraper import runner
    person = runner._upsert_person_by_name("Larry Page", source_id=None)
    entity = runner._upsert_entity_by_name(name="Alphabet Inc.", entity_type="company")
    return runner, person, entity


def _roles(person_id):
    from app.database import db
    with db.get_session() as s:
        # `since` may be null; sort on a string so a mixed list is orderable.
        return sorted(((r["role"], r["since"]) for r in s.run(
            "MATCH (p:Person {id:$id})-[r:HAS_ROLE]->() RETURN r.role AS role, r.since AS since",
            id=person_id)), key=lambda t: (t[0], t[1] or ""))


class TestOneRoleOneEdge:
    def test_an_undated_repeat_does_not_add_a_second_edge(self, graph):
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Board Member", "s1", since="1998-01-01")
        runner._upsert_role(person, entity, "Board Member", "s1")      # person path: no date

        assert _roles(person) == [("Board Member", "1998-01-01")], \
            "the undated assertion should have matched the dated one"

    def test_in_either_order(self, graph):
        # The person may be scraped before the company or after; neither order
        # may produce two edges.
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Board Member", "s1")
        runner._upsert_role(person, entity, "Board Member", "s1", since="1998-01-01")

        # …and the later, better-informed assertion fills in the date rather than
        # leaving an undated edge behind.
        assert _roles(person) == [("Board Member", "1998-01-01")]

    def test_a_second_tenure_after_an_ended_one_is_its_own_edge(self, graph):
        # Larry Page really was CEO of Google twice. Collapsing those would lose
        # the fact the timeline is drawn from.
        runner, person, entity = graph
        runner._upsert_role(person, entity, "CEO", "s1", since="1998-01-01", until="2001-08-01")
        runner._upsert_role(person, entity, "CEO", "s1", since="2011-04-04")

        assert _roles(person) == [("CEO", "1998-01-01"), ("CEO", "2011-04-04")]

    def test_two_open_assertions_days_apart_are_one_seat_from_the_earlier_day(self, graph):
        # R. Martin Chávez: the SEC's Form 3 dates his Alphabet seat 2022-07-11,
        # Wikidata 2022-07-14. Dev drew two open "Director" rows.
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Director", "s1", since="2022-07-11")
        runner._upsert_role(person, entity, "Board Member", "s2", since="2022-07-14")

        assert _roles(person) == [("Director", "2022-07-11")]

    def test_in_the_other_order_too(self, graph):
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Board Member", "s2", since="2022-07-14")
        runner._upsert_role(person, entity, "Director", "s1", since="2022-07-11")

        assert _roles(person) == [("Board Member", "2022-07-11")]

    def test_an_end_stated_for_a_touching_period_ends_the_open_seat(self, graph):
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Director", "s1", since="2022-07-11")
        runner._upsert_role(person, entity, "Board Member", "s2",
                            since="2022-07-14", until="2025-03-01")

        from app.database import db
        with db.get_session() as s:
            rows = list(s.run("MATCH (p:Person {id:$id})-[r:HAS_ROLE]->() "
                              "RETURN r.since AS since, r.until AS until", id=person))
        assert [(r["since"], r["until"]) for r in rows] == [("2022-07-11", "2025-03-01")]

    def test_different_roles_stay_apart(self, graph):
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Board Member", "s1")
        runner._upsert_role(person, entity, "Founder", "s1")

        assert [r for r, _ in _roles(person)] == ["Board Member", "Founder"]


class TestBothDirectionsAgreeOnTheName:
    def test_the_person_path_uses_the_canonical_role_names(self):
        # Free-typed strings are what caused this: "Board member" and
        # "Board Member" are different edges and look identical in a UI list.
        from app.models.relationship import RoleType
        from app.scraper.wikidata import PERSON_LINK_PROPS, OWNER_ROLE

        canonical = {r.value for r in RoleType}
        for prop, role in PERSON_LINK_PROPS.items():
            if role == OWNER_ROLE:
                continue          # ownership, not a job title
            assert role in canonical, f"{prop} writes {role!r}, which is not a RoleType"


class TestOneDateTwoPrecisions:
    """Wikidata holds Ryan Roslansky's LinkedIn CEO statement twice — 2020-06-01
    and "June 2020" — and the timeline showed him twice in 2020."""

    def test_a_month_and_its_day_are_one_seat_keeping_the_day(self, graph):
        runner, person, entity = graph
        runner._upsert_role(person, entity, "CEO", "s1", since="2020-06-01")
        runner._upsert_role(person, entity, "CEO", "s1", since="2020-06-00")
        assert _roles(person) == [("CEO", "2020-06-01")]

    def test_in_either_order(self, graph):
        runner, person, entity = graph
        runner._upsert_role(person, entity, "CEO", "s1", since="2020-06-00")
        runner._upsert_role(person, entity, "CEO", "s1", since="2020-06-01")
        assert _roles(person) == [("CEO", "2020-06-01")], "the day replaces the month"

    def test_a_statutory_day_and_a_community_year_are_one_board_seat(self, graph):
        # Charlie Scharf: SEC "Director" since 2014-10-01, Wikidata "Board
        # Member" since 2014 — two rows on Microsoft's board.
        runner, person, entity = graph
        runner._upsert_role(person, entity, "Director", "s1", since="2014-10-01",
                            credibility_score=98)
        runner._upsert_role(person, entity, "Board Member", "s2", since="2014-00-00")
        assert _roles(person) == [("Director", "2014-10-01")]

    def test_an_end_reaches_the_seat_stored_at_the_other_precision(self, graph):
        from app.database import db
        runner, person, entity = graph
        runner._upsert_role(person, entity, "CEO", "s1", since="2020-06-01")
        runner._upsert_role(person, entity, "CEO", "s1", since="2020-06-00",
                            until="2024-01-00")
        with db.get_session() as s:
            rows = list(s.run("MATCH (p:Person {id:$id})-[r:HAS_ROLE]->() "
                              "RETURN r.since AS since, r.until AS until", id=person))
        assert [(r["since"], r["until"]) for r in rows] == [("2020-06-01", "2024-01-00")]

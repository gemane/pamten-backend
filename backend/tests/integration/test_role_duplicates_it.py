"""
One role, one edge — even when two scrapes describe it differently.

Larry Page and Elon Musk each appeared twice on the same board after being
scraped from both directions. Two causes, both here:

* the person path spelled the role "Board member" while the company path writes
  "Board Member", and two strings are two edges;
* the company path knows *when* someone joined ("since 1998") and the person path
  does not, and the dedup key was role **plus** since — so an undated assertion
  of a role already recorded created a second edge beside it.

A dated tenure is still its own edge: people do hold the same post twice.
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

    def test_a_second_dated_tenure_is_still_its_own_edge(self, graph):
        # Larry Page really was CEO of Google twice. Collapsing those would lose
        # the fact the timeline is drawn from.
        runner, person, entity = graph
        runner._upsert_role(person, entity, "CEO", "s1", since="1998-01-01")
        runner._upsert_role(person, entity, "CEO", "s1", since="2011-04-04")

        assert _roles(person) == [("CEO", "1998-01-01"), ("CEO", "2011-04-04")]

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

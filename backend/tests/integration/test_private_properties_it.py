"""`Person.match_keys` — what the dedupe tells namesakes apart by (an
identity-verification day, later a hashed postcode; findings item 124) — is
served by NO read: walked over every public GET route the app declares, the
same list the suppression walk keeps complete, against a real ArcadeDB."""
import pytest

from app.db.arcadedb import run_sql
from tests.integration.test_suppression_reads_it import WALK, P, _body, client  # noqa: F401 - `client` is the fixture

pytestmark = pytest.mark.integration

KEYS = ["v:2026-01-05", "p:0f3a9c11d2e47b60"]
SECRETS = ("match_keys", "2026-01-05", "0f3a9c11d2e47b60")


def _give_keys():
    for pid in (P, "p-seat"):
        run_sql("UPDATE Person SET match_keys = :k WHERE id = :id", {"k": KEYS, "id": pid})


def test_the_keys_are_stored_and_no_public_read_serves_them(client):  # noqa: F811
    _give_keys()
    stored = run_sql("SELECT match_keys FROM Person WHERE id = :id", {"id": P})[0]["match_keys"]
    assert stored == KEYS                                   # they are there to leak
    leaks = {}
    for calls in WALK.values():
        for url, _ in calls:
            r = client.get(url)
            assert r.status_code < 500, (url, r.status_code)
            found = [s for s in SECRETS if s in _body(r)] if r.status_code == 200 else []
            if found:
                leaks[url] = found
    assert leaks == {}


def test_a_node_read_whole_does_not_carry_them_and_a_named_read_does(client):  # noqa: F811
    from app.database import db
    _give_keys()
    with db.get_session() as session:
        whole = dict(session.run("MATCH (p:Person {id: $id}) RETURN p", id=P).single()["p"])
        named = session.run("MATCH (p:Person {id: $id}) RETURN p.match_keys AS k", id=P).single().get("k")
    assert "match_keys" not in whole and whole["full_name"] == "Zebedee Quillfeather"
    assert named == KEYS


def test_the_duplicate_scan_lists_its_members_without_them(client):  # noqa: F811
    from app.routers.persons import scan_duplicate_groups
    _give_keys()
    run_sql("INSERT INTO Person SET id = 'p-twin', full_name = 'Visible Person', match_keys = :k", {"k": KEYS})
    groups = scan_duplicate_groups()
    members = [m for g in groups for m in g["members"]]
    assert members and all("match_keys" not in m for m in members)

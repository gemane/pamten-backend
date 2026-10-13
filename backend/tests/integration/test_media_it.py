"""
Real-ArcadeDB integration tests for the image lookup's record read.

The media router reads two fields off a node and follows a merge forwarding
address on a miss; the mocked suite cannot tell whether that Cypher runs on
the real dialect or whether a MergedId row is really followed. Wikimedia is
never reached: only the database side is under test here.

Skipped unless ARCADEDB_IT_URL is configured — see conftest.py.
"""
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.integration

LOGO = "https://upload.wikimedia.org/wikipedia/commons/thumb/b/bb/Tesla_T_symbol.svg/250px-Tesla_T_symbol.svg.png"


def test_the_record_read_returns_the_two_pointers(it_db):
    from app.routers.media import _record
    it_db.run_command("CREATE (:Entity {id: 'e1', name: 'Tesla', logo_url: $u})", {"u": LOGO})
    it_db.run_command("CREATE (:Person {id: 'p1', full_name: 'A B', wikipedia_url: 'https://en.wikipedia.org/wiki/A_B'})")
    it_db.run_command("CREATE (:Person {id: 'p2', full_name: 'C D'})")
    assert _record("entity", "e1") == {"logo_url": LOGO, "wikipedia_url": None}
    assert _record("person", "p1") == {"logo_url": None, "wikipedia_url": "https://en.wikipedia.org/wiki/A_B"}
    assert _record("person", "p2") == {"logo_url": None, "wikipedia_url": None}


def test_a_merged_away_id_is_followed_to_the_survivor(it_db):
    from app.merged_ids import record_merge
    from app.routers.media import _record
    it_db.run_command("CREATE (:Entity {id: 'e-survivor', name: 'Tesla', logo_url: $u})", {"u": LOGO})
    from app.database import db
    with db.get_session() as s:
        record_merge(s, "e-gone", "e-survivor")
    assert _record("entity", "e-gone")["logo_url"] == LOGO


def test_a_missing_node_is_404(it_db):
    from app.routers.media import _record
    with pytest.raises(HTTPException) as exc:
        _record("entity", "nobody")
    assert exc.value.status_code == 404

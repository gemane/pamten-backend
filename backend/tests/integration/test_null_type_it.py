"""An entity without a `type` is not a voting group.

ArcadeDB 26.10.1 made comparisons three-valued: `type <> 'voting_group'` no
longer matches a null type (26.7.3 matched it), and 1,298 of the dev graph's
6,064 entities have none. Every reader that fences off voting groups must still
see them — on both engines, which is why this runs against the real database.
"""
import pytest

from app.quality import _identity
from app.scraper.maintenance import _duplicate_name_groups, backfill_entity_countries
from app.routers.entities import get_entities_without_country
from app.routers.federation import build_export

pytestmark = pytest.mark.integration


def _seed(it_db):
    # two typeless namesakes (one with a CIK), a typed company, a voting group
    it_db.run_command("CREATE (:Entity {id:'bare', name:'Bare Co', name_normalized:'bare co', sec_cik:'0000000001'})")
    it_db.run_command("CREATE (:Entity {id:'bare2', name:'Bare Co', name_normalized:'bare co'})")
    it_db.run_command("CREATE (:Entity {id:'co', name:'Typed Co', name_normalized:'typed co', type:'company', country:'DE'})")
    it_db.run_command("CREATE (:Entity {id:'grp', name:'Voting group - X', name_normalized:'voting group x', type:'voting_group'})")


def test_the_quality_report_counts_a_typeless_entity_and_never_a_group(it_db):
    _seed(it_db)
    i = _identity()
    assert i["entities"] == 3                      # bare, bare2, co — not the group


def test_the_duplicate_scan_sees_typeless_namesakes(it_db):
    _seed(it_db)
    assert ("bare co", 2) in _duplicate_name_groups()


def test_the_country_backfill_offers_a_typeless_entity_and_not_the_group(it_db):
    _seed(it_db)
    asked = []
    def fetch(cik):
        asked.append(cik)
        return {}
    res = backfill_entity_countries(fetch=fetch)
    assert res["candidates"] == 2                   # bare + bare2 have no country; co has one; grp is no candidate
    assert asked == ["0000000001"]


def test_the_missing_country_list_names_the_typeless_entity_and_not_the_group(it_db):
    _seed(it_db)
    ids = {r["id"] for r in get_entities_without_country(basis="jurisdiction", limit=200)}
    assert ids == {"bare", "bare2"}


def test_the_federation_export_carries_the_typeless_entity_and_not_the_group(it_db):
    _seed(it_db)
    names = sorted(e["name"] for e in build_export()["entities"])
    assert names == ["Bare Co", "Bare Co", "Typed Co"]

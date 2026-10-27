"""An entity without a `type` is seen by every reader that walks the entities.

ArcadeDB 26.10.1 made comparisons three-valued: a `<>` fence on `type` dropped
every row without one (1,298 of the dev graph's 6,064 entities), which is how
the quality report, the duplicate scan, the country backfill, the missing-country
list and the federation export all went silent on them. The fence was there to
keep 13D voting groups out; since 2026-10-26 a group is its own vertex type
(VotingGroup), the fence is gone, and a typeless entity is just an entity.
"""
import pytest

from app.quality import _identity
from app.scraper.maintenance import _duplicate_name_groups
from app.routers.entities import get_entities_without_country
from app.routers.federation import build_export

pytestmark = pytest.mark.integration


def _seed(it_db):
    # two typeless namesakes (one with a CIK), a typed company, a voting group of its own type
    it_db.run_command("CREATE (:Entity {id:'bare', name:'Bare Co', name_normalized:'bare co', sec_cik:'0000000001'})")
    it_db.run_command("CREATE (:Entity {id:'bare2', name:'Bare Co', name_normalized:'bare co'})")
    it_db.run_command("CREATE (:Entity {id:'co', name:'Typed Co', name_normalized:'typed co', type:'company', country:'DE'})")
    it_db.run_command("CREATE (:VotingGroup {id:'grp', name:'Voting group · 2 parties', name_normalized:'voting group x', type:'voting_group'})")


def test_the_quality_report_counts_a_typeless_entity_and_never_a_group(it_db):
    _seed(it_db)
    i = _identity()
    assert i["entities"] == 3                      # bare, bare2, co — the group is no Entity


def test_the_duplicate_scan_sees_typeless_namesakes(it_db):
    _seed(it_db)
    assert ("bare co", 2) in _duplicate_name_groups()


def test_the_missing_country_list_names_the_typeless_entity_and_not_the_group(it_db):
    _seed(it_db)
    ids = {r["id"] for r in get_entities_without_country(basis="jurisdiction", limit=200)}
    assert ids == {"bare", "bare2"}


def test_the_federation_export_carries_the_typeless_entity_and_not_the_group(it_db):
    _seed(it_db)
    names = sorted(e["name"] for e in build_export()["entities"])
    assert names == ["Bare Co", "Bare Co", "Typed Co"]

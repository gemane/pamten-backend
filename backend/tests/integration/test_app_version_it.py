"""The version policy against a real ArcadeDB.

The read and write go through an UPSERT on a UNIQUE key — the shape this codebase
has repeatedly got wrong against a mocked session — and the whole feature is
worthless if the policy an admin sets is not the policy clients are told about.
"""
import pytest

from app.routers.app_version import app_version, set_app_version, PolicyUpdate, VersionPolicy

pytestmark = pytest.mark.integration


def test_a_policy_set_by_an_admin_is_what_clients_are_told(it_db):
    set_app_version(PolicyUpdate(ios=VersionPolicy(
        min_supported="2.0.0", latest="2.4.0", store_url="https://apps.example/owlgraph")))

    old = app_version(platform="ios", version="1.9.9")
    assert old["update_required"] is True
    assert old["store_url"] == "https://apps.example/owlgraph"

    current = app_version(platform="ios", version="2.4.0")
    assert current["update_required"] is False and current["update_available"] is False


def test_updating_replaces_rather_than_stacking(it_db):
    """UPSERT on the unique key — a second write must not leave two policy rows,
    which would make the answer depend on which one is read."""
    set_app_version(PolicyUpdate(ios=VersionPolicy(min_supported="2.0.0")))
    set_app_version(PolicyUpdate(ios=VersionPolicy(min_supported="3.0.0")))

    assert app_version(platform="ios", version="2.5.0")["update_required"] is True
    rows = it_db.run_command("MATCH (s:AppSetting) RETURN count(s) AS c")
    assert rows[0]["c"] == 1


def test_a_platform_dropped_from_the_policy_stops_blocking(it_db):
    """Full replace, not merge: removing iOS must actually remove it, or a
    rollback would leave the old minimum quietly in force."""
    set_app_version(PolicyUpdate(ios=VersionPolicy(min_supported="9.9.9")))
    assert app_version(platform="ios", version="1.0.0")["update_required"] is True

    set_app_version(PolicyUpdate(android=VersionPolicy(min_supported="1.0.0")))
    assert app_version(platform="ios", version="1.0.0")["update_required"] is False


def test_no_policy_row_means_nobody_is_blocked(it_db):
    assert app_version(platform="ios", version="0.0.1")["update_required"] is False

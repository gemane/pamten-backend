"""Unit tests for federation that don't need a database."""
import pytest


def test_import_snapshot_rejects_unknown_format():
    from app.routers.federation import import_snapshot
    with pytest.raises(ValueError):
        import_snapshot({"format": "some-other-standard", "entities": []}, "Peer: X", 60)


def test_export_constants():
    from app.routers import federation
    assert federation.EXPORT_FORMAT == "pamten-federation"
    assert federation.EXPORT_VERSION == 1


def test_status_disabled_reports_off(monkeypatch):
    from app.config import settings
    from app.routers.federation import federation_status
    monkeypatch.setattr(settings, "FEDERATION_ENABLED", False)
    st = federation_status(_={"role": "contributor"})
    assert st["enabled"] is False and st["entities"] == 0

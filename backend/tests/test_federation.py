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


def test_sign_verify_roundtrip_and_tamper(monkeypatch):
    from app.config import settings
    from app import federation_keys as fk

    priv, pub = fk.generate_keypair()
    monkeypatch.setattr(settings, "FEDERATION_SIGNING_KEY", priv)

    payload = {"format": "pamten-federation", "version": 1, "entities": [{"name": "A"}]}
    env = fk.sign(payload)
    assert env["algorithm"] == "ed25519"
    assert env["key_id"] == fk.fingerprint(pub)

    signed = {**payload, **env}
    assert fk.verify(signed, pub) is True                       # valid

    tampered = {**signed, "entities": [{"name": "B"}]}          # payload changed
    assert fk.verify(tampered, pub) is False

    _, other_pub = fk.generate_keypair()
    assert fk.verify(signed, other_pub) is False                # wrong key


def test_sign_noop_without_key(monkeypatch):
    from app.config import settings
    from app import federation_keys as fk
    monkeypatch.setattr(settings, "FEDERATION_SIGNING_KEY", "")
    assert fk.sign({"format": "x"}) == {}
    assert fk.public_key_b64() is None

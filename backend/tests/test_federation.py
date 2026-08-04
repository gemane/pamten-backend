"""Unit tests for federation that don't need a database."""
import pytest
from fastapi import HTTPException


def test_import_snapshot_rejects_unknown_format():
    from app.routers.federation import import_snapshot
    with pytest.raises(ValueError):
        import_snapshot({"format": "some-other-standard", "entities": []}, "Peer: X", 60)


def test_export_constants():
    from app.routers import federation
    assert federation.EXPORT_FORMAT == "owlgraph-federation"
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

    payload = {"format": "owlgraph-federation", "version": 1, "entities": [{"name": "A"}]}
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


# ── SSRF guard (_validate_peer_url) ──────────────────────────────────────────

class TestValidatePeerUrl:
    def setup_method(self):
        from app.routers.federation import _validate_peer_url
        self.validate = _validate_peer_url

    def _bad(self, url: str):
        """Assert that url is rejected with HTTP 400."""
        with pytest.raises(HTTPException) as ei:
            self.validate(url)
        assert ei.value.status_code == 400

    # ── must accept valid public HTTPS URLs ───────────────────────────────────

    def test_accepts_public_https_domain(self):
        self.validate("https://peer.example.com")

    def test_accepts_public_https_with_path(self):
        self.validate("https://owlgraph.example.org/api")

    def test_accepts_public_https_with_port(self):
        self.validate("https://peer.example.com:8443")

    def test_accepts_public_ipv4(self):
        # A real public IP (Cloudflare DNS) must be allowed
        self.validate("https://1.1.1.1")

    # ── scheme enforcement ────────────────────────────────────────────────────

    def test_rejects_http(self):
        self._bad("http://peer.example.com")

    def test_rejects_file_scheme(self):
        self._bad("file:///etc/passwd")

    def test_rejects_ftp_scheme(self):
        self._bad("ftp://peer.example.com")

    # ── localhost / loopback ──────────────────────────────────────────────────

    def test_rejects_localhost(self):
        self._bad("https://localhost")

    def test_rejects_localhost_with_port(self):
        self._bad("https://localhost:2480")

    def test_rejects_127_loopback(self):
        self._bad("https://127.0.0.1")

    def test_rejects_127_loopback_any_host(self):
        self._bad("https://127.1.2.3")

    def test_rejects_ipv6_loopback(self):
        self._bad("https://[::1]")

    # ── link-local (cloud metadata endpoints) ────────────────────────────────

    def test_rejects_aws_metadata_endpoint(self):
        self._bad("https://169.254.169.254")

    def test_rejects_link_local_range(self):
        self._bad("https://169.254.0.1")

    # ── RFC-1918 private ranges ───────────────────────────────────────────────

    def test_rejects_10_private(self):
        self._bad("https://10.0.0.1")

    def test_rejects_172_private(self):
        self._bad("https://172.16.0.1")

    def test_rejects_192_168_private(self):
        self._bad("https://192.168.1.1")

    # ── internal hostnames / DNS ──────────────────────────────────────────────

    def test_rejects_dotlocal(self):
        self._bad("https://peer.local")

    def test_rejects_dotinternal(self):
        self._bad("https://arcadedb.internal")

    def test_rejects_dotlocalhost(self):
        self._bad("https://service.localhost")

    def test_rejects_bare_hostname_no_dot(self):
        # Single-label hostnames resolve via internal DNS (e.g. Docker service names)
        self._bad("https://arcadedb")

    def test_rejects_bare_hostname_no_dot_with_port(self):
        self._bad("https://postgres:5432")

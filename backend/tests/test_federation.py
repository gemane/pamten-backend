"""Unit tests for federation that don't need a database."""
import pytest
from fastapi import HTTPException


# ── The hold ──────────────────────────────────────────────────────────────────
#
# Federation is parked behind two independent locks: FEDERATION_ENABLED in the
# environment, and FEDERATION_ON_HOLD in the source. The env var alone protects
# something serious — the export ships every Person in the database to any peer,
# and the published privacy pages omit federation *because* it is off — so a
# dashboard misclick must not be able to turn it back on.
#
# These tests are what make the second lock real rather than decorative.

def test_the_hold_is_engaged_by_default():
    """Lifting it should turn a test red, not slip through as a one-word diff."""
    from app.routers import federation
    assert federation.FEDERATION_ON_HOLD is True


def test_the_env_var_alone_cannot_turn_federation_on(monkeypatch):
    """The whole point. Someone sets FEDERATION_ENABLED=true in Render, not knowing
    the history — every route must still refuse."""
    from app.config import settings
    from app.routers import federation

    monkeypatch.setattr(settings, "FEDERATION_ENABLED", True)   # the accident
    for call in (lambda: federation.export_snapshot(_={"role": "admin"}),
                 lambda: federation.list_peers(_={"role": "admin"}),
                 lambda: federation.public_key(_={"role": "admin"})):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403
        assert "on hold" in exc.value.detail


def test_status_reports_off_even_with_the_env_var_on(monkeypatch):
    """The one endpoint that answers instead of refusing must not claim to be on
    while every other route 404s — nor count what it would publish."""
    from app.config import settings
    from app.routers.federation import federation_status

    monkeypatch.setattr(settings, "FEDERATION_ENABLED", True)
    assert federation_status(_={"role": "contributor"}) == {
        "enabled": False, "entities": 0, "persons": 0, "ownerships": 0}


def test_the_routes_are_not_mounted_at_all(client):
    """Unmounted, not merely disabled: nothing should advertise a capability that
    is switched off, and there is no route left for a misconfiguration to open."""
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "federation" in p]
    assert client.get("/v1/federation/export").status_code == 404
    assert client.get("/federation/export").status_code == 404


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


def test_export_carries_register_id_everywhere(fake_db):
    """Entities AND both ownership refs — a snapshot that exports the id on the
    node but not on the edge refs reconciles the edge by name (bug class of PR
    #280's search_text: one path taught, its sibling not)."""
    from app.routers.federation import build_export
    fake_db.queue(
        [{"name": "Alpha", "type": "company", "country": "PA", "founded": None,
          "wd": None, "cik": None, "lei": None, "ch": None, "rid": "RA000123:99"}],
        [],  # persons
        [{"a_wd": None, "a_cik": None, "a_lei": None, "a_ch": None,
          "a_rid": "RA000123:99", "a_name": "Alpha", "a_full": None,
          "b_wd": None, "b_cik": None, "b_lei": None, "b_ch": None,
          "b_rid": "RA000123:77", "b_name": "Beta",
          "stake": 10.0, "otype": "minority", "surl": None, "sdate": None}],
        [],  # person-owned edges
    )
    snap = build_export()
    assert snap["entities"][0]["register_id"] == "RA000123:99"
    own = snap["ownerships"][0]
    assert own["owner"]["register_id"] == "RA000123:99"
    assert own["owned"]["register_id"] == "RA000123:77"


def test_import_resolves_and_creates_with_register_id(fake_db):
    from app.routers.federation import _upsert_entity
    # resolve: register_id must be among the lookups tried (it is the first
    # non-empty id on this ref, so the first lookup is the register_id one)
    fake_db.queue([{"id": "found"}])
    got = _upsert_entity(fake_db, {"name": "Alpha", "register_id": "RA000123:99"}, "src", 70)
    assert got == "found"
    assert any("e.register_id = $v" in c for c, _ in fake_db.calls)

    # create: a peer-imported node must CARRY the id, or the next snapshot
    # re-creates it beside the first
    fake_db.calls.clear()
    got = _upsert_entity(fake_db, {"name": "Gamma", "register_id": "RA000123:55"}, "src", 70)
    assert got and got != "found"
    create = next(c for c, _ in fake_db.calls if c.startswith("CREATE"))
    assert "register_id:$rid" in create
    _, params = next((c, p) for c, p in fake_db.calls if c.startswith("CREATE"))
    assert params["rid"] == "RA000123:55"

"""Real-ArcadeDB test that a user's language is stored and used.

The stored preference is the whole point of the design: a password reset goes to
the account holder, who may not be whoever triggered it — the requester could be
a stranger probing for accounts, and their UI language says nothing about what
the recipient reads. That only works if registration actually persists the field
and the lookup actually reads it back, which needs a real database.
"""
import pytest

from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration


def _register(client, email: str, language: str | None):
    headers = {"X-Owlgraph-Language": language} if language else {}
    return client.post("/auth/register",
                       json={"email": email, "password": "Zt9mQ2vLp4rK"},
                       headers=headers)


def test_registration_stores_the_ui_language(it_db, client):
    _register(client, "de-user@example.com", "de")
    rows = run_sql("SELECT language FROM User WHERE email = 'de-user@example.com'")
    assert rows and rows[0]["language"] == "de"


def test_a_regional_tag_is_stored_as_the_base_language(it_db, client):
    # de-AT and de-CH read the same catalogue; the region would just be noise.
    _register(client, "at-user@example.com", "de-AT")
    rows = run_sql("SELECT language FROM User WHERE email = 'at-user@example.com'")
    assert rows[0]["language"] == "de"


def test_no_header_falls_back_to_english(it_db, client):
    _register(client, "plain@example.com", None)
    rows = run_sql("SELECT language FROM User WHERE email = 'plain@example.com'")
    assert rows[0]["language"] == "en"


def test_an_unsupported_language_is_stored_as_english(it_db, client):
    _register(client, "fr-user@example.com", "fr")
    rows = run_sql("SELECT language FROM User WHERE email = 'fr-user@example.com'")
    assert rows[0]["language"] == "en"


def test_a_reset_uses_the_owners_language_not_the_requesters(it_db, client, monkeypatch):
    """The scenario the design exists for: a German account holder, and a reset
    requested from an English session."""
    sent: list = []
    from app.auth import router as auth_router
    monkeypatch.setattr(auth_router, "send_password_reset_email",
                        lambda to, token, language=None: sent.append((to, language)))

    _register(client, "owner@example.com", "de")
    client.post("/auth/forgot-password", json={"email": "owner@example.com"},
                headers={"X-Owlgraph-Language": "en"})

    assert sent, "no reset email was issued"
    assert sent[0][1] == "de", f"used the requester's language: {sent[0][1]}"

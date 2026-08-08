"""Emails in the reader's language.

The site is fully localized but the emails were hardcoded English, so a German
user got a German UI and an English verification mail.

Two things are covered: that the catalogue is complete and consistent, and that
each message actually comes out translated — subject, body and call to action,
in both the text and HTML parts.
"""
from unittest.mock import patch

import pytest

from app.notifications import email as mail
from app.notifications.i18n import DEFAULT_LANGUAGE, SUPPORTED, TRANSLATIONS, normalize, t


# ── The catalogue ─────────────────────────────────────────────────────────────

class TestCatalogue:
    def test_every_language_has_every_key(self):
        """A key missing from one language would render English into an
        otherwise-German email, or worse the raw key."""
        english = set(TRANSLATIONS[DEFAULT_LANGUAGE])
        for lang in SUPPORTED:
            assert set(TRANSLATIONS[lang]) == english, f"{lang} differs from {DEFAULT_LANGUAGE}"

    def test_languages_match_what_the_ui_offers(self):
        assert set(SUPPORTED) == {"en", "de", "es"}

    def test_placeholders_survive_translation(self):
        # A translator dropping {hours} would produce "The link expires in hours".
        for lang in SUPPORTED:
            assert "{hours}" in TRANSLATIONS[lang]["verify.expiry"]
            assert "{minutes}" in TRANSLATIONS[lang]["reset.expiry"]

    def test_nothing_is_left_untranslated(self):
        """Every German and Spanish string should differ from the English one —
        an identical string is almost always a forgotten translation."""
        for lang in ("de", "es"):
            same = [k for k, v in TRANSLATIONS[lang].items()
                    if v == TRANSLATIONS[DEFAULT_LANGUAGE][k]]
            assert not same, f"{lang} still English for: {same}"


class TestNormalize:
    @pytest.mark.parametrize("given,expected", [
        ("de", "de"), ("DE", "de"),
        ("de-AT", "de"), ("de_CH", "de"),   # region never changes the catalogue
        ("es-419", "es"),
        ("fr", "en"), ("", "en"), (None, "en"), ("nonsense", "en"),
    ])
    def test_maps_onto_something_we_have(self, given, expected):
        assert normalize(given) == expected


class TestLookup:
    def test_translates(self):
        assert t("verify.subject", "de") != t("verify.subject", "en")

    def test_fills_placeholders(self):
        assert "24" in t("verify.expiry", "de", hours=24)

    def test_unknown_language_falls_back_to_english(self):
        assert t("verify.subject", "fr") == t("verify.subject", "en")

    def test_unknown_key_returns_the_key_rather_than_raising(self):
        # A background send task must never die on a missing string.
        assert t("nope.missing", "de") == "nope.missing"


# ── The messages ──────────────────────────────────────────────────────────────

def _sent(fn, *args, language=None):
    sender = type("S", (), {"send": lambda self, to, subject, text, html=None:
                            captured.update(to=to, subject=subject, text=text, html=html)})()
    captured: dict = {}
    with patch.object(mail, "get_email_sender", return_value=sender):
        fn(*args, language) if language is not None else fn(*args)
    return captured


class TestVerificationEmail:
    def test_is_german_when_asked(self):
        msg = _sent(mail.send_verification_email, "u@example.com", "tok", language="de")
        assert "Bestätigen" in msg["subject"]
        assert "Willkommen bei Owlgraph" in msg["text"]
        assert "E-Mail-Adresse bestätigen" in msg["html"]

    def test_is_english_by_default(self):
        msg = _sent(mail.send_verification_email, "u@example.com", "tok")
        assert msg["subject"] == "Verify your Owlgraph email"

    def test_still_carries_the_link_in_both_parts(self):
        msg = _sent(mail.send_verification_email, "u@example.com", "tok", language="de")
        assert "action=verify-email" in msg["text"]
        assert "action=verify-email" in msg["html"]

    def test_expiry_hours_are_filled_in(self):
        msg = _sent(mail.send_verification_email, "u@example.com", "tok", language="de")
        assert "{hours}" not in msg["text"]


class TestPasswordResetEmail:
    def test_is_spanish_when_asked(self):
        msg = _sent(mail.send_password_reset_email, "u@example.com", "tok", language="es")
        assert "contraseña" in msg["subject"]
        assert "action=reset-password" in msg["text"]

    def test_expiry_minutes_are_filled_in(self):
        msg = _sent(mail.send_password_reset_email, "u@example.com", "tok", language="de")
        assert "{minutes}" not in msg["text"] and "{minutes}" not in msg["html"]


class TestAccountExistsEmail:
    def test_is_german_when_asked(self):
        msg = _sent(mail.send_account_exists_email, "u@example.com", language="de")
        assert "Owlgraph-Konto" in msg["subject"]
        assert "Hallo," in msg["text"]

    def test_links_to_the_app(self):
        msg = _sent(mail.send_account_exists_email, "u@example.com", language="de")
        from app.config import settings
        assert settings.APP_BASE_URL.rstrip("/") in msg["html"]

"""
Unit tests for the provider-agnostic email sender (app/notifications/email.py).
No network: the SMTP path is exercised with a mocked smtplib.SMTP.
"""
from unittest.mock import MagicMock, patch

from app.notifications import email as mail


def test_suite_never_uses_smtp_by_default():
    # Safety net: the test suite must resolve to the console backend, so no test
    # can open a real SMTP connection (once sent verification mail to the fake
    # addresses new@x.com / first@x.com when a local .env had Gmail creds).
    assert mail._resolve_backend() == "console"


def test_backend_auto_selects_console_without_smtp_host(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "")
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    assert isinstance(mail.get_email_sender(), mail.ConsoleBackend)


def test_backend_auto_selects_smtp_when_host_set(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.gmail.com")
    assert isinstance(mail.get_email_sender(), mail.SMTPBackend)


def test_explicit_backend_overrides_auto(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "console")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.gmail.com")   # would be smtp on auto
    assert isinstance(mail.get_email_sender(), mail.ConsoleBackend)


def test_console_backend_does_not_raise():
    mail.ConsoleBackend().send("a@x.com", "Subj", "body text")


def test_smtp_backend_logs_in_and_sends(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(settings, "SMTP_STARTTLS", True)

    smtp = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = smtp
    with patch("app.notifications.email.smtplib.SMTP", return_value=ctx) as SMTP:
        mail.SMTPBackend().send("to@x.com", "Verify", "click the link")

    SMTP.assert_called_once_with("smtp.gmail.com", 587, timeout=10)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("me@gmail.com", "app-password")
    smtp.send_message.assert_called_once()


def test_verification_email_contains_action_link(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "APP_BASE_URL", "https://app.example")
    sent = {}
    with patch.object(mail, "get_email_sender") as factory:
        factory.return_value.send = lambda to, subject, text, html=None: sent.update(
            to=to, subject=subject, text=text, html=html)
        mail.send_verification_email("u@x.com", "TOK123")
    assert "https://app.example/?action=verify-email&token=TOK123" in sent["text"]
    assert "TOK123" in sent["html"]

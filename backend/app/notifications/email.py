"""
Provider-agnostic transactional email — verification and password-reset messages.

Two backends, chosen by ``settings.EMAIL_BACKEND``:
  * ``smtp``    — send via ``settings.SMTP_*`` using the stdlib ``smtplib`` (works
                  with Gmail: host ``smtp.gmail.com``, port 587, an App Password).
  * ``console`` — log the whole message (subject, recipient, body incl. the link)
                  instead of sending. This is the default when ``SMTP_HOST`` is
                  unset, so local dev and the test suite need no credentials.

Only stdlib is used (``smtplib`` + ``email.message``) — no third-party dependency.
"""
import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger(__name__)


def _resolve_backend() -> str:
    """'smtp' or 'console'. Explicit EMAIL_BACKEND wins; otherwise auto: 'smtp'
    when an SMTP host is configured, else 'console'."""
    choice = (settings.EMAIL_BACKEND or "").strip().lower()
    if choice in ("smtp", "console"):
        return choice
    return "smtp" if settings.SMTP_HOST.strip() else "console"


def _from_address() -> str:
    return settings.EMAIL_FROM.strip() or settings.SMTP_USERNAME.strip() or "no-reply@pamten.local"


class EmailSender:
    """Base sender interface."""

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        raise NotImplementedError


class ConsoleBackend(EmailSender):
    """Logs the email instead of sending — dev/test default (no secrets needed)."""

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        log.info(
            "[email:console] would send\n  from: %s\n  to:   %s\n  subj: %s\n%s",
            _from_address(), to, subject, text,
        )


class SMTPBackend(EmailSender):
    """Sends via SMTP (STARTTLS by default) using the stdlib smtplib."""

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = _from_address()
        msg["To"] = to
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        log.info("[email:smtp] sent %r to %s", subject, to)


def get_email_sender() -> EmailSender:
    return SMTPBackend() if _resolve_backend() == "smtp" else ConsoleBackend()


# ── Message templates ─────────────────────────────────────────────────────────

def _link(action: str, token: str) -> str:
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/?action={action}&token={token}"


def send_verification_email(to: str, token: str) -> None:
    url = _link("verify-email", token)
    subject = "Verify your Pamten email"
    text = (
        "Welcome to Pamten.\n\n"
        "Please confirm your email address by opening this link:\n\n"
        f"{url}\n\n"
        f"The link expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours. "
        "If you didn't create an account, you can ignore this message.\n"
    )
    html = (
        f"<p>Welcome to Pamten.</p>"
        f"<p>Please confirm your email address:</p>"
        f'<p><a href="{url}">Verify my email</a></p>'
        f"<p>The link expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours. "
        f"If you didn't create an account, you can ignore this message.</p>"
    )
    get_email_sender().send(to, subject, text, html)


def send_password_reset_email(to: str, token: str) -> None:
    url = _link("reset-password", token)
    subject = "Reset your Pamten password"
    text = (
        "We received a request to reset your Pamten password.\n\n"
        "Open this link to choose a new password:\n\n"
        f"{url}\n\n"
        f"The link expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
        "If you didn't request this, you can ignore this message — your password "
        "stays unchanged.\n"
    )
    html = (
        f"<p>We received a request to reset your Pamten password.</p>"
        f'<p><a href="{url}">Choose a new password</a></p>'
        f"<p>The link expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
        f"If you didn't request this, you can ignore this message — your password "
        f"stays unchanged.</p>"
    )
    get_email_sender().send(to, subject, text, html)

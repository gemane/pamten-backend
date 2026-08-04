"""
Provider-agnostic transactional email — verification and password-reset messages.

Backends, chosen by ``settings.EMAIL_BACKEND``:
  * ``console`` — log the whole message (subject, recipient, body incl. the link)
                  instead of sending. The default when nothing is configured, so
                  local dev and the test suite need no credentials.
  * ``smtp``    — send via ``settings.SMTP_*`` using the stdlib ``smtplib`` (works
                  with Gmail locally, but Render blocks outbound SMTP).
  * ``resend``  — send via the Resend HTTPS API (``RESEND_API_KEY``). Works from
                  Render (HTTPS, port 443). ``EMAIL_FROM`` must be a Resend-verified
                  sender (a verified domain, or ``onboarding@resend.dev`` in test
                  mode — which only delivers to the account owner).

Uses only stdlib + ``httpx`` (already a dependency) — no provider SDKs.
"""
import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_KNOWN_BACKENDS = ("console", "smtp", "resend")


def _resolve_backend() -> str:
    """The backend name. An explicit, recognised EMAIL_BACKEND wins; otherwise
    auto: 'smtp' when an SMTP host is configured, else 'console'."""
    choice = (settings.EMAIL_BACKEND or "").strip().lower()
    if choice in _KNOWN_BACKENDS:
        return choice
    return "smtp" if settings.SMTP_HOST.strip() else "console"


def _from_address() -> str:
    return settings.EMAIL_FROM.strip() or settings.SMTP_USERNAME.strip() or "no-reply@owlgraph.local"


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

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        log.info("[email:smtp] sent %r to %s", subject, to)


class ResendBackend(EmailSender):
    """Sends via the Resend HTTPS API — works from Render (unlike SMTP)."""

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        payload: dict = {"from": _from_address(), "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json=payload, timeout=10,
        )
        resp.raise_for_status()
        log.info("[email:resend] sent %r to %s", subject, to)


def get_email_sender() -> EmailSender:
    backend = _resolve_backend()
    if backend == "resend":
        return ResendBackend()
    if backend == "smtp":
        return SMTPBackend()
    return ConsoleBackend()


# ── Message templates ─────────────────────────────────────────────────────────

def _link(action: str, token: str) -> str:
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/?action={action}&token={token}"


def send_verification_email(to: str, token: str) -> None:
    url = _link("verify-email", token)
    subject = "Verify your Owlgraph email"
    text = (
        "Welcome to Owlgraph.\n\n"
        "Please confirm your email address by opening this link:\n\n"
        f"{url}\n\n"
        f"The link expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours. "
        "If you didn't create an account, you can ignore this message.\n"
    )
    html = (
        f"<p>Welcome to Owlgraph.</p>"
        f"<p>Please confirm your email address:</p>"
        f'<p><a href="{url}">Verify my email</a></p>'
        f"<p>The link expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours. "
        f"If you didn't create an account, you can ignore this message.</p>"
    )
    get_email_sender().send(to, subject, text, html)


def send_account_exists_email(to: str) -> None:
    """Tell an existing user that someone tried to register with their address.

    Sent in place of a verification email when a registration attempt uses an
    email that is already in the database — so the response to the caller stays
    generic (no enumeration) while the real account owner is notified and can act.
    """
    login_url = settings.APP_BASE_URL.rstrip("/")
    subject = "Someone tried to create an Owlgraph account with your email"
    text = (
        "Hi,\n\n"
        "Someone just tried to register a new Owlgraph account using your email address. "
        "You already have an account, so no new account was created.\n\n"
        f"If that was you, you can log in here: {login_url}\n\n"
        "If you've forgotten your password, use the 'Forgot password' link on the login page.\n\n"
        "If it wasn't you, no action is needed — your account is unchanged.\n"
    )
    html = (
        "<p>Hi,</p>"
        "<p>Someone just tried to register a new Owlgraph account using your email address. "
        "You already have an account, so no new account was created.</p>"
        f'<p>If that was you, <a href="{login_url}">log in here</a>. '
        "If you've forgotten your password, use the <em>Forgot password</em> link on the login page.</p>"
        "<p>If it wasn't you, no action is needed — your account is unchanged.</p>"
    )
    get_email_sender().send(to, subject, text, html)


def send_password_reset_email(to: str, token: str) -> None:
    url = _link("reset-password", token)
    subject = "Reset your Owlgraph password"
    text = (
        "We received a request to reset your Owlgraph password.\n\n"
        "Open this link to choose a new password:\n\n"
        f"{url}\n\n"
        f"The link expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
        "If you didn't request this, you can ignore this message — your password "
        "stays unchanged.\n"
    )
    html = (
        f"<p>We received a request to reset your Owlgraph password.</p>"
        f'<p><a href="{url}">Choose a new password</a></p>'
        f"<p>The link expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
        f"If you didn't request this, you can ignore this message — your password "
        f"stays unchanged.</p>"
    )
    get_email_sender().send(to, subject, text, html)

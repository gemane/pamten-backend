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
  * ``scaleway`` — send via the Scaleway Transactional Email HTTPS API
                  (``SCALEWAY_SECRET_KEY`` + ``SCALEWAY_PROJECT_ID``). Works from
                  Render. ``EMAIL_FROM`` must be an address on a domain verified
                  in Scaleway TEM (SPF/DKIM/DMARC). See ``ScalewayBackend``.

Uses only stdlib + ``httpx`` (already a dependency) — no provider SDKs.
"""
import logging
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr

import httpx

from app.config import settings
from app.notifications.i18n import t

log = logging.getLogger(__name__)

_KNOWN_BACKENDS = ("console", "smtp", "resend", "scaleway")


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


def _raise_for_status(resp: "httpx.Response", provider: str) -> None:
    """Fail loudly, and say what the provider actually objected to.

    `raise_for_status()` alone reports "400 Bad Request" and throws the body
    away — but the body is the whole message: an unverified sender domain, a
    project id that does not match the key, a malformed address. Diagnosing a
    send failure without it means guessing, so the body (truncated) is logged
    and folded into the exception before it propagates.
    """
    if resp.is_success:
        return
    detail = (resp.text or "").strip().replace("\n", " ")[:400]
    log.error("[email:%s] send rejected — HTTP %s: %s", provider, resp.status_code, detail)
    raise httpx.HTTPStatusError(
        f"{provider} rejected the send — HTTP {resp.status_code}: {detail}",
        request=resp.request, response=resp,
    )


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
        _raise_for_status(resp, "resend")
        log.info("[email:resend] sent %r to %s", subject, to)


class ScalewayBackend(EmailSender):
    """Sends via the Scaleway Transactional Email HTTPS API — works from Render.

    Request (Scaleway TEM ``v1alpha1``)::

        POST https://api.scaleway.com/transactional-email/v1alpha1/regions/{region}/emails
        X-Auth-Token: <SCALEWAY_SECRET_KEY>          # the API key's Secret Key
        Content-Type: application/json
        {
          "from":       {"email": "noreply@owlgraph.org", "name": "Owlgraph"},
          "to":         [{"email": "user@example.com"}],
          "subject":    "...",
          "text":       "...",
          "html":       "...",          # omitted when no HTML part
          "project_id": "<SCALEWAY_PROJECT_ID>"
        }

    ``region`` is ``SCALEWAY_TEM_REGION`` (default ``fr-par``). The ``from`` email
    must be on a domain verified in Scaleway TEM (SPF/DKIM/DMARC), or the API
    rejects the send. ``EMAIL_FROM`` may be a bare address or ``Name <addr>`` —
    parseaddr splits it into the structured ``from`` object either way.
    """

    _BASE = "https://api.scaleway.com/transactional-email/v1alpha1/regions"

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        display_name, addr = parseaddr(_from_address())
        sender: dict = {"email": addr}
        if display_name:
            sender["name"] = display_name

        payload: dict = {
            "from":       sender,
            "to":         [{"email": to}],
            "subject":    subject,
            "text":       text,
            "project_id": settings.SCALEWAY_PROJECT_ID,
        }
        if html:
            payload["html"] = html

        region = settings.SCALEWAY_TEM_REGION.strip() or "fr-par"
        resp = httpx.post(
            f"{self._BASE}/{region}/emails",
            headers={"X-Auth-Token": settings.SCALEWAY_SECRET_KEY},
            json=payload, timeout=10,
        )
        _raise_for_status(resp, "scaleway")
        log.info("[email:scaleway] sent %r to %s (region=%s)", subject, to, region)


def get_email_sender() -> EmailSender:
    backend = _resolve_backend()
    if backend == "resend":
        return ResendBackend()
    if backend == "scaleway":
        return ScalewayBackend()
    if backend == "smtp":
        return SMTPBackend()
    return ConsoleBackend()


# ── Message templates ─────────────────────────────────────────────────────────

def _link(action: str, token: str) -> str:
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/?action={action}&token={token}"


def send_verification_email(to: str, token: str, language: str | None = None) -> None:
    url = _link("verify-email", token)
    hours = settings.EMAIL_VERIFY_TTL_HOURS
    text = (
        f"{t('verify.intro', language)}\n\n"
        f"{t('verify.instruction', language)}\n\n"
        f"{url}\n\n"
        f"{t('verify.expiry', language, hours=hours)} {t('verify.ignore', language)}\n"
    )
    html = (
        f"<p>{t('verify.intro', language)}</p>"
        f"<p>{t('verify.instruction', language)}</p>"
        f'<p><a href="{url}">{t("verify.cta", language)}</a></p>'
        f"<p>{t('verify.expiry', language, hours=hours)} {t('verify.ignore', language)}</p>"
    )
    get_email_sender().send(to, t("verify.subject", language), text, html)


def send_account_exists_email(to: str, language: str | None = None) -> None:
    """Tell an existing user that someone tried to register with their address.

    Sent in place of a verification email when a registration attempt uses an
    email that is already in the database — so the response to the caller stays
    generic (no enumeration) while the real account owner is notified and can act.

    Written in the *account owner's* language, not the requester's: the person
    triggering this may be a stranger probing for accounts, and their UI language
    says nothing about what the recipient reads.
    """
    login_url = settings.APP_BASE_URL.rstrip("/")
    text = (
        f"{t('exists.greeting', language)}\n\n"
        f"{t('exists.body', language)}\n\n"
        f"{t('exists.login', language)} {login_url}\n\n"
        f"{t('exists.forgot', language)}\n\n"
        f"{t('exists.noaction', language)}\n"
    )
    html = (
        f"<p>{t('exists.greeting', language)}</p>"
        f"<p>{t('exists.body', language)}</p>"
        f'<p>{t("exists.login", language)} <a href="{login_url}">'
        f'{t("exists.login_cta", language)}</a>. {t("exists.forgot", language)}</p>'
        f"<p>{t('exists.noaction', language)}</p>"
    )
    get_email_sender().send(to, t("exists.subject", language), text, html)


def send_password_reset_email(to: str, token: str, language: str | None = None) -> None:
    url = _link("reset-password", token)
    minutes = settings.PASSWORD_RESET_TTL_MINUTES
    text = (
        f"{t('reset.intro', language)}\n\n"
        f"{t('reset.instruction', language)}\n\n"
        f"{url}\n\n"
        f"{t('reset.expiry', language, minutes=minutes)} {t('reset.ignore', language)}\n"
    )
    html = (
        f"<p>{t('reset.intro', language)}</p>"
        f'<p><a href="{url}">{t("reset.cta", language)}</a></p>'
        f"<p>{t('reset.expiry', language, minutes=minutes)} {t('reset.ignore', language)}</p>"
    )
    get_email_sender().send(to, t("reset.subject", language), text, html)

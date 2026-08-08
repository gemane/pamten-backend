"""Translations for outbound email.

The frontend has its own catalogue (src/i18n/locales/*.json) but the emails are
composed server-side, so the strings have to live here too. Kept deliberately
small — three messages — rather than reaching for a translation framework.

Languages match the UI's: English, German, Spanish.

Formatting placeholders use ``{}``-style named fields, filled by the senders in
notifications/email.py. Every language must carry every key; there is a test
that fails if one drifts.
"""
from __future__ import annotations

DEFAULT_LANGUAGE = "en"

#: The languages the UI offers. Anything else falls back to DEFAULT_LANGUAGE.
SUPPORTED = ("en", "de", "es")


def normalize(language: str | None) -> str:
    """Map an incoming language tag onto one we actually have.

    Accepts the bare tag the UI sends ("de") and the regional forms a browser
    might ("de-AT", "de-CH") — the region never changes which catalogue applies,
    so it is dropped. Unknown or missing falls back to English rather than
    raising: an email in the wrong language is a small problem, an exception in
    a background send task is a silent lost email.
    """
    if not language:
        return DEFAULT_LANGUAGE
    base = language.strip().lower().split("-")[0].split("_")[0]
    return base if base in SUPPORTED else DEFAULT_LANGUAGE


TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "verify.subject": "Verify your Owlgraph email",
        "verify.intro": "Welcome to Owlgraph.",
        "verify.instruction": "Please confirm your email address by opening this link:",
        "verify.cta": "Verify my email",
        "verify.expiry": "The link expires in {hours} hours.",
        "verify.ignore": "If you didn't create an account, you can ignore this message.",

        "reset.subject": "Reset your Owlgraph password",
        "reset.intro": "We received a request to reset your Owlgraph password.",
        "reset.instruction": "Open this link to choose a new password:",
        "reset.cta": "Choose a new password",
        "reset.expiry": "The link expires in {minutes} minutes.",
        "reset.ignore": "If you didn't request this, you can ignore this message — "
                        "your password stays unchanged.",

        "exists.subject": "Someone tried to create an Owlgraph account with your email",
        "exists.greeting": "Hi,",
        "exists.body": "Someone just tried to register a new Owlgraph account using your "
                       "email address. You already have an account, so no new account was created.",
        "exists.login": "If that was you, you can log in here:",
        "exists.login_cta": "log in here",
        "exists.forgot": "If you've forgotten your password, use the 'Forgot password' link "
                         "on the login page.",
        "exists.noaction": "If it wasn't you, no action is needed — your account is unchanged.",
    },
    "de": {
        "verify.subject": "Bestätigen Sie Ihre Owlgraph-E-Mail-Adresse",
        "verify.intro": "Willkommen bei Owlgraph.",
        "verify.instruction": "Bitte bestätigen Sie Ihre E-Mail-Adresse über diesen Link:",
        "verify.cta": "E-Mail-Adresse bestätigen",
        "verify.expiry": "Der Link ist {hours} Stunden gültig.",
        "verify.ignore": "Falls Sie kein Konto erstellt haben, können Sie diese Nachricht "
                         "ignorieren.",

        "reset.subject": "Owlgraph-Passwort zurücksetzen",
        "reset.intro": "Wir haben eine Anfrage erhalten, Ihr Owlgraph-Passwort zurückzusetzen.",
        "reset.instruction": "Öffnen Sie diesen Link, um ein neues Passwort zu wählen:",
        "reset.cta": "Neues Passwort wählen",
        "reset.expiry": "Der Link ist {minutes} Minuten gültig.",
        "reset.ignore": "Falls Sie das nicht angefordert haben, können Sie diese Nachricht "
                        "ignorieren — Ihr Passwort bleibt unverändert.",

        "exists.subject": "Jemand wollte ein Owlgraph-Konto mit Ihrer E-Mail-Adresse anlegen",
        "exists.greeting": "Hallo,",
        "exists.body": "Jemand hat gerade versucht, ein neues Owlgraph-Konto mit Ihrer "
                       "E-Mail-Adresse zu registrieren. Sie haben bereits ein Konto, daher "
                       "wurde kein neues angelegt.",
        "exists.login": "Falls Sie das waren, können Sie sich hier anmelden:",
        "exists.login_cta": "hier anmelden",
        "exists.forgot": "Falls Sie Ihr Passwort vergessen haben, nutzen Sie den Link "
                         "„Passwort vergessen“ auf der Anmeldeseite.",
        "exists.noaction": "Falls Sie das nicht waren, müssen Sie nichts tun — Ihr Konto "
                           "bleibt unverändert.",
    },
    "es": {
        "verify.subject": "Verifica tu correo de Owlgraph",
        "verify.intro": "Te damos la bienvenida a Owlgraph.",
        "verify.instruction": "Confirma tu dirección de correo abriendo este enlace:",
        "verify.cta": "Verificar mi correo",
        "verify.expiry": "El enlace caduca en {hours} horas.",
        "verify.ignore": "Si no has creado una cuenta, puedes ignorar este mensaje.",

        "reset.subject": "Restablece tu contraseña de Owlgraph",
        "reset.intro": "Hemos recibido una solicitud para restablecer tu contraseña de Owlgraph.",
        "reset.instruction": "Abre este enlace para elegir una nueva contraseña:",
        "reset.cta": "Elegir una nueva contraseña",
        "reset.expiry": "El enlace caduca en {minutes} minutos.",
        "reset.ignore": "Si no lo has solicitado, puedes ignorar este mensaje: tu contraseña "
                        "no cambiará.",

        "exists.subject": "Alguien intentó crear una cuenta de Owlgraph con tu correo",
        "exists.greeting": "Hola:",
        "exists.body": "Alguien acaba de intentar registrar una nueva cuenta de Owlgraph con tu "
                       "dirección de correo. Ya tienes una cuenta, así que no se creó ninguna nueva.",
        "exists.login": "Si has sido tú, puedes iniciar sesión aquí:",
        "exists.login_cta": "iniciar sesión aquí",
        "exists.forgot": "Si has olvidado tu contraseña, usa el enlace «¿Olvidaste tu contraseña?» "
                         "en la página de inicio de sesión.",
        "exists.noaction": "Si no has sido tú, no hace falta hacer nada: tu cuenta no ha cambiado.",
    },
}


def t(key: str, language: str | None = None, **fields) -> str:
    """One translated string, with any ``{placeholders}`` filled in.

    Falls back to English for a missing language *and* for a key missing from an
    otherwise-known language, so a half-finished translation degrades to English
    rather than rendering a raw key into someone's inbox.
    """
    lang = normalize(language)
    text = TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
    return text.format(**fields) if fields else text

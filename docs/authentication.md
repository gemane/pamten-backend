# Authentication

How accounts, tokens and roles work. The endpoints are listed in [`api-reference.md`](api-reference.md); every setting in [`configuration.md`](configuration.md).

Access tokens are JWTs signed with `SECRET_KEY` (HS256, **15-minute** expiry — `ACCESS_TOKEN_EXPIRE_MINUTES`). Nothing is consulted when one is presented, so the TTL *is* the blast radius of a stolen token — hence a short one. `SECRET_KEY` must be **at least 32 characters**; the app refuses to start with a shorter one, in any mode, since short HS256 keys are brute-forceable. Set a strong random key in production:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**Sessions (refresh tokens).** A 15-minute access token would mean logging in four times an hour, so sessions are carried by a separate refresh token — the opposite shape to a JWT: opaque, stored server-side (hashed, in `RefreshToken`), and therefore **revocable**. It is delivered as an `httpOnly` cookie, so JavaScript cannot read it and an XSS bug cannot steal the long-lived credential.

`POST /auth/refresh` trades the cookie for a new access token and **rotates** the cookie; `POST /auth/logout` revokes it. Both are unauthenticated by design — they are called exactly when the access token has expired. Refreshing re-reads the account, so a role change or a deletion takes effect within one access-token lifetime rather than lingering for the life of the session.

Rotation makes theft detectable. A stolen token works only until the real client refreshes; whoever refreshes second presents an already-consumed token, which cannot happen normally. That burns the whole **family** (the chain of successors from one login), logging out thief and victim alike — other sessions, being separate families, are untouched. Session lifetime is **absolute** (`REFRESH_TOKEN_EXPIRE_DAYS`, default 30): rotation renews the secret, not the clock.

Sessions end on password change (other sessions only — the caller stays signed in), password reset (all), and account deletion (rows erased, not just revoked). Already-issued access tokens are the exception in every case: they are checked against nothing, so another session stays usable until its token expires. Bounding that window is what the short TTL is for.

> **Same-site requirement.** The cookie is `SameSite=Lax`, so the frontend and API must share one registrable domain — `dev.owlgraph.org` + `api-dev.owlgraph.org`. It is *not* sent from a differently-registered origin, so a frontend dev server on `localhost` talking to a deployed API gets no session refresh and falls back to re-login every 15 minutes; run the backend locally (with `REFRESH_COOKIE_SECURE=false` for plain http) to avoid that. The old `*.onrender.com` pair could never have worked: `onrender.com` is on the Public Suffix List, making those two hosts separate sites.

**The admin account.** Set `ADMIN_EMAIL` + `ADMIN_PASSWORD` and that account is provisioned as admin on every startup (created if it doesn't exist, from the hashed `ADMIN_PASSWORD`; never overwritten if it already exists). With `ADMIN_EMAIL` set, self-registration only ever creates `viewer`s. This is the recommended way to get an admin on a fresh database — it avoids the race where, on an empty DB, whoever hits the public `/auth/register` first would become admin. If `ADMIN_EMAIL` is **not** set, the legacy fallback applies: the first account to register becomes admin.

**Email verification & password reset.** Registration creates the account with `email_verified=false`, emails a verification link, and — with `REQUIRE_EMAIL_VERIFICATION=true` (default) — **blocks login until the email is verified** (`403 email_not_verified`, which the UI turns into a *resend* prompt). `POST /auth/forgot-password` emails a reset link and always returns `200` (no account-existence leak); `POST /auth/reset-password` sets the new password. The verify/reset links are **self-contained signed JWTs** (purpose-scoped, TTL-bounded) — no server-side token table; a reset link embeds a fingerprint of the current password hash so it **self-invalidates** once used. The env/bootstrap admin and the legacy first-user admin are stamped verified so they're never locked out.

**Changing a password.** A signed-in user rotates their own with `POST /auth/change-password` (`{current_password, new_password}`) — proving ownership with the current password rather than an email round-trip, so it works even where outbound SMTP is blocked. Reusing the current password is rejected. Other sessions are revoked and the caller's is re-issued, so a change evicts anyone else holding the old password without logging you out of the browser you are using.

For the operator case — a password that must be rotated but nobody knows it, or an inbox that can't receive the reset mail — use the CLI:

```bash
python manage.py set-password someone@example.com   # prompts twice, hidden input
```

Note that `ADMIN_PASSWORD` is **not** a way to change a password: `bootstrap_admin()` only creates a *missing* account and never overwrites an existing one, so editing that env var on an account that already exists has no effect at all.

**Deleting an account.** `DELETE /auth/me` with `{password}` permanently deletes the caller's own account — required by both app stores for any app that allows account creation, and the mechanism behind a GDPR erasure request. It re-authenticates with the password, so a stolen access token alone isn't enough.

It removes the `User` node (and with it the password hash, TOTP secret and recovery codes) plus the account's rate-limit counters. Flags the user filed are **anonymised, not deleted** (`reporter_kind='deleted'`, reporter id and fingerprint cleared): the reports are about companies rather than about the reporter, so deleting them would silently rewrite moderation history — only the link back to the person is severed.

Two cases are refused rather than half-honoured:

- the **`ADMIN_EMAIL` bootstrap account**, because `bootstrap_admin()` recreates it on the next startup — the deletion would quietly undo itself. Unset `ADMIN_EMAIL` first.
- the **last remaining admin**, which would leave the instance with nobody able to administer it. Promote someone else first.

Admins can still delete *other* users with `DELETE /auth/users/{id}`; that path does not anonymise flags.

**Password policy.** The rules live in one place, `app/auth/password_policy.py` (`password_policy_error`), shared by register, reset, change, and `manage.py set-password`, so the API and CLI can't drift apart. User-chosen passwords must be **at least 8 characters** and are checked against a **common-password blocklist** — the top ~10k most common passwords (`app/auth/common_passwords.txt`, from [SecLists](https://github.com/danielmiessler/SecLists), MIT licence), so weak-but-long-enough choices like `password123` are rejected. There are deliberately **no character-composition rules** (per NIST SP 800-63B). Online guessing is further limited by login rate-limiting (5 attempts / 15 min) and optional TOTP MFA. The env `ADMIN_PASSWORD` bootstrap is exempt (operator-set, not user input).

**Email transport** is provider-agnostic (`app/notifications/email.py`) — `EMAIL_BACKEND` picks the backend: `scaleway` (Scaleway Transactional Email HTTPS API via `SCALEWAY_SECRET_KEY` + `SCALEWAY_PROJECT_ID`, region `SCALEWAY_TEM_REGION` default `fr-par`), `resend` (Resend HTTPS API via `RESEND_API_KEY`), `smtp` (stdlib `smtplib`; works with Gmail locally), or the default `console` (logs the message + link, so local dev and tests need no credentials). The two HTTPS backends both work from **Render, which blocks outbound SMTP**. Adding another provider is a ~15-line `EmailSender` subclass. `EMAIL_FROM` must be a sender the backend accepts — for Scaleway/Resend an address on a domain verified with the provider (SPF/DKIM/DMARC), e.g. `noreply@owlgraph.org`; `APP_BASE_URL` sets the origin used in the emailed links.

**Two-factor auth (TOTP).** Users can enable an authenticator-app second factor (`POST /auth/mfa/setup` → scan the returned `otpauth://` QR → `POST /auth/mfa/enable` with a code, which returns 10 one-time **recovery codes**). Once enabled, `login` returns `{mfa_required, mfa_token}` instead of an access token; the client exchanges that pending token + a 6-digit code (or a recovery code) at `POST /auth/mfa/verify` for the real token. TOTP uses `cryptography` (RFC 6238, ±1 step for clock drift); the secret + hashed recovery codes live as schemaless `User` props. `mfa/verify` is rate-limited per account, and disabling requires a current code.

Protected routes use FastAPI `Depends`:

| Dependency | Requirement |
|---|---|
| `get_current_user` | Any valid JWT |
| `require_admin` | Role must be `admin` |
| `require_moderator` | Role must be `admin` or `moderator` (data-moderation / verification queue) |
| `require_contributor` | Role must be `admin` or `contributor` |

from pydantic_settings import BaseSettings
from pydantic import model_validator

INSECURE_DEFAULT_SECRET_KEY = "change-me-in-production-use-a-long-random-string"


class Settings(BaseSettings):
    ARCADEDB_URL:      str
    ARCADEDB_USERNAME: str
    ARCADEDB_PASSWORD: str
    ARCADEDB_DATABASE: str = "owlgraph"
    APP_NAME:          str  = "Ownership Platform"
    DEBUG:             bool = False
    SCRAPER_ENABLED:                  bool = False
    SCRAPER_WIKIDATA_ENABLED:         bool = True
    SCRAPER_SEC_EDGAR_ENABLED:        bool = False
    SCRAPER_OPENCORPORATES_ENABLED:   bool = False
    SCRAPER_BODS_GLEIF_ENABLED:       bool = False
    SCRAPER_BODS_UK_PSC_ENABLED:      bool = False
    # Where the BODS importer spills its on-disk id maps (and downloads) for a
    # multi-GB import. Defaults to the system temp dir — but on boxes where /tmp
    # is a small tmpfs (RAM), a full UK PSC import fills it and SQLite corrupts
    # ("database or disk is full" / "disk image is malformed"). Point this at a
    # path on a real disk with tens of GB free for large imports.
    SCRAPER_TMP_DIR:                  str | None = None
    # After each run-all scrape, auto-merge high-confidence duplicate persons that
    # different sources spelled differently. Only high-confidence, non-distinct
    # groups are merged; medium/low go to the review panel. Set false to disable.
    SCRAPER_AUTODEDUP_ENABLED:        bool = True
    # On-demand (instant-source) scraping: a company already scraped within this
    # many days is served straight from the DB — no re-scrape — unless the user
    # forces it. "Never on-demand scraped" and "> this many days" both re-scrape.
    SCRAPER_ONDEMAND_TTL_DAYS:        int  = 30
    # Hard cooldown after any on-demand scrape: for this many hours the company
    # cannot be scraped again — not even with a forced "Refresh from sources" — so
    # the external sources aren't hammered. A deepen to a not-yet-reached depth is a
    # continuation of the same enrichment and is still allowed.
    SCRAPER_ONDEMAND_COOLDOWN_HOURS:  int  = 24
    # /search resilience: when the FULL_TEXT index returns nothing, fall back to a
    # bounded substring scan on the name so a degraded/incomplete FULL_TEXT index
    # (e.g. after an interrupted bulk-load) can't silently hide companies that ARE
    # in the DB. Only runs on the no-result path; set false on very large DBs where
    # the un-indexed scan is too costly (repair the index instead — see
    # `manage.py rebuild-search --hard`).
    SEARCH_SUBSTRING_FALLBACK:        bool = True
    # Trusted-peer federation (step 1: one-way pull of a peer's published export,
    # reconciled through the duplicate scan). Off by default; opt in per instance.
    FEDERATION_ENABLED:               bool = False
    # Ed25519 private signing key (base64 32-byte seed) for signing exports so
    # peers can verify a pull is genuinely ours. Generate with
    # `python manage.py gen-federation-key`; keep it secret (env only).
    FEDERATION_SIGNING_KEY:           str  = ""
    OPENCORPORATES_API_KEY:           str  = ""
    SECRET_KEY:                       str  = INSECURE_DEFAULT_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES:      int  = 60 * 12  # 12 hours
    CORS_ORIGINS:                     str  = ""
    # Admin bootstrap: when ADMIN_EMAIL is set, that account is provisioned as
    # admin on startup (created if missing, from ADMIN_PASSWORD) — so a fresh DB
    # has an admin without the "first person to /register becomes admin" race.
    # With ADMIN_EMAIL set, self-registration never grants admin.
    ADMIN_EMAIL:                      str | None = None
    ADMIN_PASSWORD:                   str | None = None
    # ── Transactional email (verification + password reset) ───────────────────
    # Provider-agnostic sender. EMAIL_BACKEND selects the transport: "smtp" sends
    # via SMTP_*, "console" just logs the message (with the link) — the default
    # when SMTP_HOST is unset, so local dev + tests need no secrets. For Gmail:
    # SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USERNAME=<you@gmail.com>,
    # SMTP_PASSWORD=<Google App Password> (needs 2-Step Verification enabled).
    EMAIL_BACKEND:                    str  = ""     # "console" | "smtp" | "resend" | "" (auto)
    RESEND_API_KEY:                   str  = ""     # secret — env only; for EMAIL_BACKEND=resend
    SMTP_HOST:                        str  = ""
    SMTP_PORT:                        int  = 587
    SMTP_USERNAME:                    str  = ""
    SMTP_PASSWORD:                    str  = ""     # secret — env only
    SMTP_STARTTLS:                    bool = True
    EMAIL_FROM:                       str  = ""     # defaults to SMTP_USERNAME
    # Frontend origin used to build the links in verification / reset emails.
    APP_BASE_URL:                     str  = "http://localhost:5173"
    # Gate login on a verified email. Verify/reset links are self-invalidating
    # JWTs (no server-side token store); TTLs below bound their validity.
    REQUIRE_EMAIL_VERIFICATION:       bool = True
    EMAIL_VERIFY_TTL_HOURS:           int  = 24
    PASSWORD_RESET_TTL_MINUTES:       int  = 60
    # Geocoding (Nominatim / OpenStreetMap). Disabled by default; the public
    # endpoint requires a descriptive User-Agent with a contact and enforces
    # ~1 request/second, which GEOCODING_MIN_INTERVAL respects.
    GEOCODING_ENABLED:                bool  = False
    NOMINATIM_URL:                    str   = "https://nominatim.openstreetmap.org/search"
    GEOCODING_USER_AGENT:             str   = "owlgraph-ownership-platform"
    GEOCODING_CONTACT:                str   = ""
    GEOCODING_MIN_INTERVAL:           float = 1.0

    class Config:
        env_file = ".env"
        extra    = "ignore"

    @model_validator(mode="after")
    def _require_secret_key_override_in_production(self) -> "Settings":
        if not self.DEBUG and self.SECRET_KEY == INSECURE_DEFAULT_SECRET_KEY:
            raise ValueError(
                "SECRET_KEY is still set to the insecure default. "
                "Set a long random SECRET_KEY env var before running with DEBUG=False."
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()

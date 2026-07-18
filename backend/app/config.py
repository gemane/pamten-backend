from pydantic_settings import BaseSettings
from pydantic import model_validator

INSECURE_DEFAULT_SECRET_KEY = "change-me-in-production-use-a-long-random-string"


class Settings(BaseSettings):
    ARCADEDB_URL:      str
    ARCADEDB_USERNAME: str
    ARCADEDB_PASSWORD: str
    ARCADEDB_DATABASE: str = "pamten"
    APP_NAME:          str  = "Ownership Platform"
    DEBUG:             bool = False
    SCRAPER_ENABLED:                  bool = False
    SCRAPER_WIKIDATA_ENABLED:         bool = True
    SCRAPER_SEC_EDGAR_ENABLED:        bool = False
    SCRAPER_OPENCORPORATES_ENABLED:   bool = False
    SCRAPER_BODS_GLEIF_ENABLED:       bool = False
    SCRAPER_BODS_UK_PSC_ENABLED:      bool = False
    # After each run-all scrape, auto-merge high-confidence duplicate persons that
    # different sources spelled differently. Only high-confidence, non-distinct
    # groups are merged; medium/low go to the review panel. Set false to disable.
    SCRAPER_AUTODEDUP_ENABLED:        bool = True
    OPENCORPORATES_API_KEY:           str  = ""
    SECRET_KEY:                       str  = INSECURE_DEFAULT_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES:      int  = 60 * 12  # 12 hours
    CORS_ORIGINS:                     str  = ""
    # Only files inside this directory may be passed as local_file to the
    # BODS import endpoints (prevents arbitrary server file reads).
    BODS_DATA_DIR:                    str  = "/data"
    # Geocoding (Nominatim / OpenStreetMap). Disabled by default; the public
    # endpoint requires a descriptive User-Agent with a contact and enforces
    # ~1 request/second, which GEOCODING_MIN_INTERVAL respects.
    GEOCODING_ENABLED:                bool  = False
    NOMINATIM_URL:                    str   = "https://nominatim.openstreetmap.org/search"
    GEOCODING_USER_AGENT:             str   = "pamten-ownership-platform"
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

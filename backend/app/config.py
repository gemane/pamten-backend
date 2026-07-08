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
    SCRAPER_SEC_EDGAR_ENABLED:        bool = False
    SCRAPER_OPENCORPORATES_ENABLED:   bool = False
    SCRAPER_BODS_GLEIF_ENABLED:       bool = False
    SCRAPER_BODS_UK_PSC_ENABLED:      bool = False
    OPENCORPORATES_API_KEY:           str  = ""
    SECRET_KEY:                       str  = INSECURE_DEFAULT_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES:      int  = 60 * 12  # 12 hours
    CORS_ORIGINS:                     str  = ""
    # Only files inside this directory may be passed as local_file to the
    # BODS import endpoints (prevents arbitrary server file reads).
    BODS_DATA_DIR:                    str  = "/data"

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

from pydantic_settings import BaseSettings


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
    SECRET_KEY:                       str  = "change-me-in-production-use-a-long-random-string"
    ACCESS_TOKEN_EXPIRE_MINUTES:      int  = 60 * 24 * 7  # 7 days

    class Config:
        env_file = ".env"
        extra    = "ignore"


settings = Settings()

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    NEO4J_URI: str
    NEO4J_USERNAME: str
    NEO4J_PASSWORD: str
    NEO4J_DATABASE: str = "neo4j"
    APP_NAME: str = "Ownership Platform"
    DEBUG: bool = False
    SCRAPER_ENABLED: bool = False
    SCRAPER_SEC_EDGAR_ENABLED: bool = False
    SECRET_KEY: str = "change-me-in-production-use-a-long-random-string"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    class Config:
        env_file = ".env"


settings = Settings()

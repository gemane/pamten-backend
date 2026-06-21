from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    NEO4J_URI: str
    NEO4J_USERNAME: str
    NEO4J_PASSWORD: str
    NEO4J_DATABASE: str = "neo4j"
    APP_NAME: str = "Ownership Platform"
    DEBUG: bool = False

    class Config:
        env_file = ".env"


settings = Settings()

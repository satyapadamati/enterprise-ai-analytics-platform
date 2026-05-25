from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    APP_NAME: str = "Enterprise AI Analytics Platform"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"

    # CORS
    ALLOWED_ORIGINS: List[str] = ["*"]

    # Chat / LLM
    MAX_TOKENS: int = 2048
    DEFAULT_MODEL: str = "gpt-4o"

    # Snowflake
    SNOWFLAKE_ACCOUNT: str = ""
    SNOWFLAKE_USER: str = ""
    SNOWFLAKE_PASSWORD: str = ""
    SNOWFLAKE_DATABASE: str = ""
    SNOWFLAKE_SCHEMA: str = "PUBLIC"
    SNOWFLAKE_WAREHOUSE: str = ""
    SNOWFLAKE_ROLE: Optional[str] = None
    SNOWFLAKE_LOGIN_TIMEOUT: int = 30
    SNOWFLAKE_NETWORK_TIMEOUT: int = 60

    # Groq LLM
    GROQ_API_KEY: str = ""
    GROQ_API_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_DEFAULT_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_DEFAULT_TEMPERATURE: float = 0.0
    GROQ_REQUEST_TIMEOUT: int = 60
    GROQ_MAX_RETRIES: int = 3


settings = Settings()

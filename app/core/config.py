from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # --- Bootstrap Settings ---
    # These are the only settings read from the .env file.
    DATABASE_URL: str = "sqlite+aiosqlite:///./ollama_proxy.db"
    ADMIN_USER: str = "admin"
    ADMIN_PASSWORD: str = "changeme"
    PROXY_PORT: int = 8080
    SECRET_KEY: str = "dd2a57833f4a2115b02644c3c332822d5b6e405d542a2258c422fb39a8e97b10"

    # --- App Info (Hardcoded) ---
    APP_NAME: str = "Ollama Proxy Server"
    APP_VERSION: str = "9.0.0"
    LOG_LEVEL: str = "info"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = 'ignore'  # <-- THIS IS THE FIX

# This `settings` object is now only used for bootstrapping.
# The rest of the app will use settings loaded from the DB.
settings = Settings()

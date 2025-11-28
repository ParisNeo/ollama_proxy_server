from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # --- Bootstrap Settings ---
    # These are the only settings read from the .env file.
    # IMPORTANT: Never commit .env files with real credentials!
    DATABASE_URL: str = "sqlite+aiosqlite:///./ollama_proxy.db"
    ADMIN_USER: str = "admin"
    ADMIN_PASSWORD: str = "changeme"  # MUST be changed in production .env file
    PROXY_PORT: int = 8080
    SECRET_KEY: str = "CHANGE_THIS_IN_PRODUCTION"  # MUST be changed in production .env file

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

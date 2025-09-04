import os
from typing import List, Union
from pydantic import AnyHttpUrl, field_validator, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- App Settings ---
    APP_NAME: str = "Ollama Proxy Server"
    APP_VERSION: str = "8.0.0"
    LOG_LEVEL: str = "info"
    SECRET_KEY: str
    PROXY_PORT: int = 8080

    # --- Ollama Backend Servers ---
    OLLAMA_SERVERS: Union[List[AnyHttpUrl], str]

    @field_validator("OLLAMA_SERVERS", mode="before")
    @classmethod
    def assemble_ollama_servers(cls, v: Union[str, List[AnyHttpUrl]]) -> List[AnyHttpUrl]:
        if isinstance(v, str):
            return [url.strip() for url in v.split(",") if url.strip()]
        return v

    # --- Database Settings ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./ollama_proxy.db"

    # --- Admin User ---
    ADMIN_USER: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # --- Redis & Rate Limiting ---
    REDIS_URL: RedisDsn = "redis://localhost:6379/0"
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_MINUTES: int = 1

    # --- IP Access Control ---
    ALLOWED_IPS: List[str] = []
    DENIED_IPS: List[str] = []

    @field_validator("ALLOWED_IPS", "DENIED_IPS", mode="before")
    @classmethod
    def assemble_ip_lists(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [ip.strip() for ip in v.split(",") if ip.strip()]
        return v


    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
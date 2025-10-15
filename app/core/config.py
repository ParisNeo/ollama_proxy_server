import os
from typing import List, Union, Optional
from pydantic import AnyHttpUrl, field_validator, RedisDsn, model_validator
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
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_USERNAME: Optional[str] = None
    REDIS_PASSWORD: Optional[str] = None
    REDIS_URL: Optional[RedisDsn] = None

    @model_validator(mode='after')
    def assemble_redis_url(self) -> 'Settings':
        """
        Constructs Redis DSN from components if REDIS_URL is not already provided.
        This ensures backward compatibility with old .env files that have REDIS_URL.
        """
        if self.REDIS_URL is None:
            # Build the URL from components if the old REDIS_URL is not set
            if self.REDIS_USERNAME and self.REDIS_PASSWORD:
                credentials = f"{self.REDIS_USERNAME}:{self.REDIS_PASSWORD}@"
            elif self.REDIS_USERNAME:
                credentials = f"{self.REDIS_USERNAME}@"
            else:
                credentials = ""
            
            # Pydantic will validate this string against the RedisDsn type
            self.REDIS_URL = f"redis://{credentials}{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        
        return self

    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_MINUTES: int = 1

    # --- HTTP Client (Backend) Timeouts & Limits ---
    HTTPX_CONNECT_TIMEOUT: float = 10.0
    HTTPX_READ_TIMEOUT: float = 600.0
    HTTPX_WRITE_TIMEOUT: float = 600.0
    HTTPX_POOL_TIMEOUT: float = 60.0
    HTTPX_MAX_KEEPALIVE_CONNECTIONS: int = 20
    HTTPX_MAX_CONNECTIONS: int = 100
    HTTPX_KEEPALIVE_EXPIRY: float = 60.0

    # --- IP Access Control ---
    ALLOWED_IPS: List[str] = []
    DENIED_IPS: List[str] = []

    @field_validator("ALLOWED_IPS", "DENIED_IPS", mode="before")
    @classmethod
    def assemble_ip_lists(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [ip.strip() for ip in v.split(",") if ip.strip()]
        return v

    # --- Model Refresh Settings ---
    MODEL_REFRESH_INTERVAL_MINUTES: int = 10

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
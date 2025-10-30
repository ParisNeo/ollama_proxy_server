from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

class AppSettingsModel(BaseModel):
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_username: Optional[str] = None
    redis_password: Optional[str] = None

    rate_limit_requests: int = 100
    rate_limit_window_minutes: int = 1

    allowed_ips: str = ""
    denied_ips: str = ""

    model_update_interval_minutes: int = 10

    # Retry configuration for backend requests
    max_retries: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Maximum number of retry attempts when a backend server request fails"
    )
    retry_total_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        description="Total time budget (in seconds) for all retry attempts"
    )
    retry_base_delay_ms: int = Field(
        default=50,
        ge=10,
        le=5000,
        description="Base delay in milliseconds for exponential backoff between retries"
    )

    @field_validator('retry_total_timeout_seconds')
    @classmethod
    def validate_retry_timeout(cls, v: float, info) -> float:
        """Ensure retry timeout is reasonable."""
        if v <= 0:
            raise ValueError("retry_total_timeout_seconds must be positive")
        return v

    class Config:
        from_attributes = True
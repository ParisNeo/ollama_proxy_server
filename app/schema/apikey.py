"""API key Pydantic schemas for Ollama Proxy Server."""

import datetime

from pydantic import BaseModel, ConfigDict


class APIKeyBase(BaseModel):
    """Base API key schema."""

    key_name: str


class APIKeyCreate(APIKeyBase):
    """API key creation schema."""


class APIKey(APIKeyBase):
    """API key schema."""

    id: int
    key_prefix: str
    user_id: int
    expires_at: datetime.datetime | None
    is_revoked: bool
    created_at: datetime.datetime

    model_config = ConfigDict(extra="forbid")

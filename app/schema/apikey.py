import datetime
from pydantic import BaseModel


class APIKeyBase(BaseModel):
    key_name: str


class APIKeyCreate(APIKeyBase):
    pass


class APIKey(APIKeyBase):
    id: int
    key_prefix: str
    user_id: int
    expires_at: datetime.datetime | None
    is_revoked: bool
    created_at: datetime.datetime

    class Config:
        from_attributes = True
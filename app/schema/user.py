"""User schemas for Ollama Proxy Server."""

from pydantic import BaseModel


class UserBase(BaseModel):
    """Base user schema with common fields."""

    username: str


class UserCreate(UserBase):
    """User creation schema."""

    password: str


class User(UserBase):
    """Complete user schema for API responses."""

    id: int
    is_active: bool
    is_admin: bool

    class Config:
        """Pydantic configuration."""

        from_attributes = True

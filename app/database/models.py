import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    key_name = Column(String, nullable=False)
    hashed_key = Column(String, unique=True, index=True, nullable=False)
    key_prefix = Column(String, unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=True)
    is_revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="api_keys")
    usage_logs = relationship("UsageLog", back_populates="api_key", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("user_id", "key_name", name="uq_user_key_name"),)


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False)
    endpoint = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False)
    request_timestamp = Column(DateTime, default=datetime.datetime.utcnow)

    api_key = relationship("APIKey", back_populates="usage_logs")


class OllamaServer(Base):
    __tablename__ = "ollama_servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
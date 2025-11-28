import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    JSON,
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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    is_active = Column(Boolean, default=True, nullable=False)
    is_revoked = Column(Boolean, default=False, nullable=False)
    
    rate_limit_requests = Column(Integer, nullable=True)
    rate_limit_window_minutes = Column(Integer, nullable=True)

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
    server_id = Column(Integer, ForeignKey("ollama_servers.id"), nullable=True)
    model = Column(String, nullable=True, index=True)

    api_key = relationship("APIKey", back_populates="usage_logs")
    server = relationship("OllamaServer")


class OllamaServer(Base):
    __tablename__ = "ollama_servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)
    server_type = Column(String, nullable=False, default="ollama", server_default="ollama")
    encrypted_api_key = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    available_models = Column(JSON, nullable=True)
    enabled_models = Column(JSON, nullable=True)  # For OpenRouter: list of enabled model names
    models_last_updated = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def has_api_key(self) -> bool:
        return bool(self.encrypted_api_key)

class AppSettings(Base):
    __tablename__ = "app_settings"
    id = Column(Integer, primary_key=True)
    settings_data = Column(JSON, nullable=False)

class ModelMetadata(Base):
    __tablename__ = "model_metadata"
    id = Column(Integer, primary_key=True, index=True)
    model_name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    supports_images = Column(Boolean, default=False, nullable=False)
    is_code_model = Column(Boolean, default=False, nullable=False)
    is_chat_model = Column(Boolean, default=True, nullable=False)
    is_fast_model = Column(Boolean, default=False, nullable=False)
    supports_tool_calling = Column(Boolean, default=False, nullable=False)
    supports_internet = Column(Boolean, default=False, nullable=False)
    is_thinking_model = Column(Boolean, default=False, nullable=False)
    priority = Column(Integer, default=10, nullable=False)
    
    __table_args__ = (UniqueConstraint("model_name", name="uq_model_name"),)


class Conversation(Base):
    """Threaded conversation/chat thread"""
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=True)  # Auto-generated from first exchange
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
    
    # For RAG: store embedding of first exchange for similarity search
    first_exchange_embedding = Column(JSON, nullable=True)  # Vector embedding as JSON array
    
    user = relationship("User")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    """Individual message in a conversation thread"""
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String, nullable=False)  # "user", "assistant", "system"
    content = Column(String, nullable=False)
    model_name = Column(String, nullable=True)  # Which model responded (for "assistant" messages)
    message_metadata = Column(JSON, nullable=True)  # Stats, images, etc. (renamed from 'metadata' to avoid SQLAlchemy conflict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    
    # For RAG: store embedding for semantic search
    embedding = Column(JSON, nullable=True)  # Vector embedding as JSON array
    
    conversation = relationship("Conversation", back_populates="messages")
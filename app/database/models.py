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
    
    # Token usage tracking
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)

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

class ManagedInstance(Base):
    __tablename__ = "managed_instances"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    backend_type = Column(String, default="ollama") # ollama, llamacpp, vllm
    port = Column(Integer, nullable=False, unique=True)
    gpu_ids = Column(String, nullable=True)
    model_path = Column(String, nullable=True) # Used by llamacpp/vllm
    
    # llamacpp specific
    n_gpu_layers = Column(Integer, default=99)
    ctx_size = Column(Integer, default=8192)
    threads = Column(Integer, default=8)
    
    # vllm specific
    tensor_parallel_size = Column(Integer, default=1)
    
    is_enabled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ModelMetadata(Base):
    __tablename__ = "model_metadata"
    id = Column(Integer, primary_key=True, index=True)
    model_name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    supports_images = Column(Boolean, default=False, nullable=False)
    is_code_model = Column(Boolean, default=False, nullable=False)
    is_chat_model = Column(Boolean, default=True, nullable=False)
    is_fast_model = Column(Boolean, default=False, nullable=False)
    is_reasoning_model = Column(Boolean, default=False, nullable=False)
    max_context = Column(Integer, default=4096, nullable=False)
    priority = Column(Integer, default=10, nullable=False)
    
    __table_args__ = (UniqueConstraint("model_name", name="uq_model_name"),)

class EnsembleOrchestrator(Base):
    __tablename__ = "model_bundles" # Keep table name for data stability
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    parallel_participants = Column(JSON, nullable=False) # Names of models or agents
    # Legacy support: matching the physical NOT NULL constraint in older DBs
    parallel_models = Column(JSON, nullable=True) 
    master_model = Column(String, nullable=False)
    vision_processor = Column(String, nullable=True) # Model used to extract image descriptions (for multimodal bundles)
    show_monologue = Column(Boolean, default=False)
    send_status_update = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def __init__(self, **kwargs):
        # Synchronize parallel_participants and parallel_models for DB compatibility
        if 'parallel_participants' in kwargs and 'parallel_models' not in kwargs:
            kwargs['parallel_models'] = kwargs['parallel_participants']
        super().__init__(**kwargs)

class SmartRouter(Base): # Keep table name for data stability
    __tablename__ = "model_pools"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    targets = Column(JSON, nullable=False) # Names of models or agents
    strategy = Column(String, default='priority', nullable=False)
    # The model used to classify intent if fast rules fail
    classifier_model = Column(String, nullable=True) 
    # Structured as List[RuleGroup]
    rules = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class VirtualAgent(Base):
    __tablename__ = "virtual_agents"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    base_model = Column(String, nullable=False)
    system_prompt = Column(String, nullable=False) # The "Soul"
    # MCP: List of Model Context Protocol server configurations (Includes RAG, Tools, etc)
    mcp_servers = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
    allowed_models = Column(JSON, nullable=True) # Whitelist of model names
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
    supports_thinking = Column(Boolean, default=False, nullable=False)
    is_chat_model = Column(Boolean, default=True, nullable=False)
    is_embedding_model = Column(Boolean, default=False, nullable=False)
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
    report_success_failure = Column(Boolean, default=False) # NEW: Reports which agents worked
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def __init__(self, **kwargs):
        # Synchronize parallel_participants and parallel_models for DB compatibility
        if 'parallel_participants' in kwargs and 'parallel_models' not in kwargs:
            kwargs['parallel_models'] = kwargs['parallel_participants']
        super().__init__(**kwargs)

class ChainOrchestrator(Base):
    __tablename__ = "model_chains"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    steps = Column(JSON, nullable=False) # List of model names:["vision-model", "text-model"]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
    
    # LEGACY: This field is deprecated, kept for backward compatibility with old DBs
    # New code should use 'targets' instead
    models = Column(JSON, nullable=True)
    
    def __init__(self, **kwargs):
        # Sync targets to models for backward compatibility
        if 'targets' in kwargs and 'models' not in kwargs:
            kwargs['models'] = kwargs['targets']
        super().__init__(**kwargs)

class LogAnalysis(Base):
    __tablename__ = "log_analyses"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    content = Column(String, nullable=False)
    
class VisionAugmenter(Base):
    __tablename__ = "vision_augmenters"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    text_model = Column(String, nullable=False)
    vision_model = Column(String, nullable=False)
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
class MemoryEntry(Base):
    __tablename__ = "agent_memories"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_name = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    content = Column(String, nullable=False)
    importance = Column(Integer, default=50) # 0-100
    last_accessed = Column(DateTime, default=datetime.datetime.utcnow)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class MemoryEntry(Base):
    __tablename__ = "memory_entries"
    id = Column(Integer, primary_key=True, index=True)
    user_identifier = Column(String, nullable=False, index=True)
    agent_name = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    content = Column(String, nullable=False)
    importance = Column(Integer, default=50)
    last_accessed = Column(DateTime, default=datetime.datetime.utcnow)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class UserToolData(Base):
    __tablename__ = "user_tool_data"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    library_name = Column(String, nullable=False, index=True)
    key = Column(String, nullable=False, index=True)
    value = Column(JSON, nullable=False)
    is_persistent = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "library_name", "key", name="uq_user_tool_key"),)


class BotConfig(Base):
    __tablename__ = "bot_configs"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    platform = Column(String, nullable=False) # 'telegram', 'discord', 'slack'
    encrypted_token = Column(String, nullable=False)
    target_workflow = Column(String, nullable=False) # The model/workflow name to call
    is_active = Column(Boolean, default=False)
    # Extra config like specific channel IDs or server IDs
    extra_settings = Column(JSON, nullable=True) 
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Workflow(Base):
    __tablename__ = "workflows"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    graph_data = Column(JSON, nullable=False) # LiteGraph JSON state
    # Type hints for the proxy (e.g. 'chain', 'ensemble', 'router')
    workflow_type = Column(String, default="custom")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class DataStore(Base):
    __tablename__ = "datastores"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    db_path = Column(String, nullable=False)
    vectorizer_name = Column(String, nullable=False, default="tfidf")
    chunking_strategy = Column(String, default="recursive")
    chunk_size = Column(Integer, default=512)
    chunk_overlap = Column(Integer, default=50)
    vectorizer_config = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

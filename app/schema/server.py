from pydantic import BaseModel, AnyHttpUrl, Field, ConfigDict
import datetime
from typing import Literal, Optional, List

class ServerBase(BaseModel):
    name: str
    url: Optional[AnyHttpUrl] = None
    server_type: Literal["ollama", "vllm", "cloud", "novita", "openllm", "open_webui", "openrouter"] = "ollama"
    max_parallel_queries: int = Field(default=1, ge=1)

class ServerCreate(ServerBase):
    url: Optional[AnyHttpUrl] = None # Explicitly optional for local Ollama
    api_key: Optional[str] = Field(None, description="Optional API key for connecting to the server.")

class ServerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[AnyHttpUrl] = None
    server_type: Optional[Literal["ollama", "vllm", "cloud", "novita", "openllm", "open_webui"]] = None
    api_key: Optional[str] = Field(None, description="Provide a new key to update, or an empty string to remove.")
    allowed_models: Optional[List[str]] = None
    is_active: Optional[bool] = None


class Server(ServerBase):
    id: int
    is_active: bool
    has_api_key: bool = False
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
